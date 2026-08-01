# -*- coding: utf-8 -*-
"""sync/lime_vk_ads.py — кабинет VK Реклама (ads.vk.com) → lime_vk_ads_stats.

Уровень = КАМПАНИИ (ad_plans в API v2). Внимание: в API v2 «campaigns» — это ГРУППЫ
объявлений, а кампании интерфейса VK = ad_plans. Метрика размечает визиты по ad_plan_id,
поэтому расход/конверсии тянем на уровне ad_plans (campaign_id в таблице = ad_plan_id).

Паритет с Директом: расход/клики/показы + конверсии по типам (jsonb). Валюта RUB —
конверсии валюты нет; spent как в кабинете (без НДС). Rate-limit строгий: батчи id,
паузы, ретрай 429, токен кэшируется на прогон.

ENV: DATABASE_URL, VK_CLIENT_ID, VK_CLIENT_SECRET, LIME_VK_DAYS_BACK (default 14).
Запуск: python -m sync.lime_vk_ads

ВНИМАНИЕ: VK_CLIENT_ID не шарить с другими потребителями. При token_limit_exceeded
_get_token отзывает ВСЕ активные токены клиента (token/delete) — это разлогинит любую
другую систему на том же client_id.
"""
import json
import os
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import date, timedelta
from typing import Any, Dict, Tuple

import psycopg2
import psycopg2.extras

BASE = "https://ads.vk.com/api/v2"
TOKEN_URL = f"{BASE}/oauth2/token.json"
TOKEN_DELETE_URL = f"{BASE}/oauth2/token/delete.json"
STAT_BATCH = 20
RETRY_429 = 5


def _num(v: Any) -> float:
    if v is None:
        return 0.0
    s = str(v).strip().replace(",", ".")
    try:
        return float(s) if s not in ("", "--") else 0.0
    except ValueError:
        return 0.0


def _int(v: Any) -> int:
    return int(round(_num(v)))


def parse_base_stats(api_json: Dict[str, Any]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """(date, campaign_id) → базовые метрики строки. Секция total игнорируется."""
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for item in api_json.get("items", []):
        cid = str(item.get("id", "")).strip()
        if not cid:
            continue
        for row in item.get("rows", []):
            date = str(row.get("date", "")).strip()
            base = row.get("base") or {}
            vk = base.get("vk") or {}
            out[(date, cid)] = {
                "shows": _int(base.get("shows")),
                "clicks": _int(base.get("clicks")),
                "spent": round(_num(base.get("spent")), 2),
                "goals_total": _int(base.get("goals")),
                "vk_result": _int(vk.get("result")),
            }
    return out


def parse_goal_stats(api_json: Dict[str, Any]) -> Dict[Tuple[str, str], Dict[str, Dict[str, Any]]]:
    """(date, campaign_id) → {goal: {count, value, view_through}}; строки одной цели суммируются."""
    out: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}
    for item in api_json.get("items", []):
        cid = str(item.get("id", "")).strip()
        if not cid:
            continue
        for row in item.get("rows", []):
            date = str(row.get("date", "")).strip()
            bucket = out.setdefault((date, cid), {})
            for g in row.get("goals", []):
                name = str(g.get("goal", "")).strip()
                if not name:
                    continue
                agg = bucket.setdefault(name, {"count": 0, "value": 0.0, "view_through": 0})
                agg["count"] += _int(g.get("count"))
                agg["value"] = round(agg["value"] + _num(g.get("value")), 2)
                agg["view_through"] += _int(g.get("view_through_count"))
    return out


def build_rows(base_map, goals_map, campaigns_meta, cabinet: str = "") -> list:
    """Слить базу + конверсии + мету кампании в строки upsert. Ведёт база (только где есть статистика).
    cabinet — метка кабинета (login), различает несколько VK-аккаунтов в одной таблице."""
    rows = []
    for (date, cid), base in base_map.items():
        meta = campaigns_meta.get(cid, {})
        conv = goals_map.get((date, cid), {})
        rows.append({
            "date": date,
            "region": "ru",
            "cabinet": cabinet,
            "campaign_id": cid,
            "campaign_name": meta.get("name"),
            "objective": meta.get("objective"),
            "status": meta.get("status"),
            "shows": base["shows"],
            "clicks": base["clicks"],
            "spent": base["spent"],
            "goals_total": base["goals_total"],
            "vk_result": base["vk_result"],
            "conversions": json.dumps(conv, ensure_ascii=False),
        })
    return rows


def _token_form(client_id: str, secret: str) -> bytes:
    return urllib.parse.urlencode({
        "grant_type": "client_credentials", "client_id": client_id, "client_secret": secret,
    }).encode("utf-8")


def _issue_token(client_id: str, secret: str) -> str:
    req = urllib.request.Request(TOKEN_URL, data=_token_form(client_id, secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8"))["access_token"]


def _delete_tokens(client_id: str, secret: str) -> None:
    """Отозвать все активные токены клиента — освободить лимит VK."""
    req = urllib.request.Request(TOKEN_DELETE_URL, data=_token_form(client_id, secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    urllib.request.urlopen(req, timeout=40).close()


def _get_token(client_id: str, secret: str) -> str:
    """Выпустить access_token; при token_limit_exceeded — отозвать старые и повторить.

    VK Реклама лимитирует число АКТИВНЫХ токенов на клиента: каждый client_credentials даёт
    новый 24ч-токен, а крон + ручные dispatch + preview накапливают их до лимита → 403
    token_limit_exceeded. Отзыв всех токенов (token/delete) освобождает лимит; повторный
    выпуск проходит. Отзыв безопасен: токены короткоживущие и принадлежат только нашему клиенту.
    """
    try:
        return _issue_token(client_id, secret)
    except urllib.error.HTTPError as e:
        if e.code == 403 and "token_limit" in e.read().decode("utf-8", "replace"):
            _delete_tokens(client_id, secret)
            return _issue_token(client_id, secret)
        raise


def _api_get(token: str, path: str, *, _sleep=time.sleep) -> dict:
    """GET ads.vk.com с ретраем 429 (rate-limit): backoff 5·attempt сек."""
    for attempt in range(RETRY_429 + 1):
        req = urllib.request.Request(f"{BASE}/{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < RETRY_429:
                _sleep(5 * (attempt + 1))
                continue
            raise


def _campaigns_from_json(js: dict) -> dict:
    out = {}
    for it in js.get("items", []):
        cid = str(it.get("id", "")).strip()
        if cid:
            out[cid] = {"name": it.get("name"), "objective": it.get("objective"),
                        "status": it.get("status")}
    return out


def fetch_ad_plans(token: str) -> dict:
    """Кампании (ad_plan_id → мета) ВСЕХ статусов. Пагинация: VK max limit=50 на страницу.

    Уровень ad_plans = кампании в интерфейсе VK (в API v2 «campaigns» — это ГРУППЫ объявлений,
    уровнем ниже). Метрика размечает визиты по ad_plan_id, поэтому расход/конверсии берём здесь.
    Без фильтра статуса: история включает blocked/deleted кампании, которые крутились в прошлом.
    """
    out: dict = {}
    offset = 0
    while True:
        js = _api_get(token,
            f"ad_plans.json?limit=50&offset={offset}&fields=id,name,status,objective")
        out.update(_campaigns_from_json(js))
        if len(js.get("items", [])) < 50:
            break
        offset += 50
    return out


def _ad_groups_from_json(js: dict, cabinet: str) -> list:
    """Группы объявлений VK → строки справочника. entity_id = id группы (макрос `c`).

    id=0 — валидное значение (встречается в пагинации), поэтому проверяем на None, а не на
    falsy: `it.get("id", "") or ""` затирало бы id=0 пустой строкой и роняло страницу."""
    out = []
    for it in js.get("items", []):
        raw_id, raw_plan = it.get("id"), it.get("ad_plan_id")
        gid = str(raw_id).strip() if raw_id is not None else ""
        plan = str(raw_plan).strip() if raw_plan is not None else ""
        if not gid or not plan:
            continue
        out.append({"entity_id": gid, "kind": "ad_group", "ad_plan_id": plan,
                    "cabinet": cabinet, "name": it.get("name")})
    return out


def fetch_ad_groups(token: str, cabinet: str) -> list:
    """Все группы объявлений кабинета (в API v2 «campaigns»), включая неактивные.

    Без фильтра статуса: установка могла прийти по группе, которую с тех пор остановили или
    удалили — она всё равно должна резолвиться в свою кампанию. Пагинация по 50 (максимум VK).
    """
    out: list = []
    offset = 0
    while True:
        js = _api_get(token, f"campaigns.json?limit=50&offset={offset}&fields=id,name,ad_plan_id")
        out.extend(_ad_groups_from_json(js, cabinet))
        if len(js.get("items", [])) < 50:
            break
        offset += 50
        time.sleep(1)  # rate-limit
    return out


def self_plan_rows(plan_ids, cabinet: str) -> list:
    """Строки-ссылки кампании на себя: макрос `c` иногда несёт id кампании, а не группы."""
    return [{"entity_id": str(p), "kind": "ad_plan", "ad_plan_id": str(p),
             "cabinet": cabinet, "name": None} for p in plan_ids]


_UPSERT_SQL = """
    INSERT INTO lime_vk_ads_stats
      (date, region, cabinet, campaign_id, campaign_name, objective, status,
       shows, clicks, spent, goals_total, vk_result, conversions, updated_at)
    VALUES
      (%(date)s, %(region)s, %(cabinet)s, %(campaign_id)s, %(campaign_name)s, %(objective)s, %(status)s,
       %(shows)s, %(clicks)s, %(spent)s, %(goals_total)s, %(vk_result)s, %(conversions)s::jsonb, NOW())
    ON CONFLICT (date, campaign_id) DO UPDATE SET
       region = EXCLUDED.region, cabinet = EXCLUDED.cabinet, campaign_name = EXCLUDED.campaign_name,
       objective = EXCLUDED.objective, status = EXCLUDED.status,
       shows = EXCLUDED.shows, clicks = EXCLUDED.clicks, spent = EXCLUDED.spent,
       goals_total = EXCLUDED.goals_total, vk_result = EXCLUDED.vk_result,
       conversions = EXCLUDED.conversions, updated_at = NOW()
"""


# Справочник накопительный: без delete-окна. Пропавшая из выдачи группа остаётся —
# по ней есть исторические установки, которые обязаны резолвиться и завтра.
_ENTITY_UPSERT_SQL = """
    INSERT INTO lime_vk_entities (entity_id, kind, ad_plan_id, cabinet, name, updated_at)
    VALUES (%(entity_id)s, %(kind)s, %(ad_plan_id)s, %(cabinet)s, %(name)s, NOW())
    ON CONFLICT (entity_id) DO UPDATE SET
       kind = EXCLUDED.kind, ad_plan_id = EXCLUDED.ad_plan_id,
       cabinet = EXCLUDED.cabinet, name = EXCLUDED.name, updated_at = NOW()
"""


def _upsert_entities(rows: list) -> int:
    if not rows:
        return 0
    with psycopg2.connect(_pg_url(), connect_timeout=30) as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, _ENTITY_UPSERT_SQL, rows, page_size=500)
        conn.commit()
    return len(rows)


def _pg_url() -> str:
    return os.environ["DATABASE_URL"].split("?")[0]


_DELETE_WINDOW_SQL = "DELETE FROM lime_vk_ads_stats WHERE date >= %s AND date <= %s"


def _upsert(rows: list, frm: str, to: str) -> int:
    """Delete+insert по окну [frm; to]: синк владеет срезом (как lime_kz_cabinet/roistat).
    Чистит окно перед записью — иначе исчезнувшие из выдачи сущности или строки прежнего
    уровня осели бы мусором. Одна транзакция: пустой прогон окна не теряет данные безвозвратно."""
    with psycopg2.connect(_pg_url(), connect_timeout=30) as conn:
        with conn.cursor() as cur:
            cur.execute(_DELETE_WINDOW_SQL, (frm, to))
            if rows:
                psycopg2.extras.execute_batch(cur, _UPSERT_SQL, rows, page_size=500)
        conn.commit()
    return len(rows)


def _chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _cabinets() -> list:
    """Пары (client_id, secret) всех VK-кабинетов из env: базовый VK_CLIENT_ID/VK_CLIENT_SECRET +
    пронумерованные VK_CLIENT_ID_N/VK_CLIENT_SECRET_N (N=2,3,4,...). Каждый кабинет = своя пара
    (client_id+secret — единица приложения VK, секрет не шарится). Метка кабинета = login аккаунта."""
    out = []
    cid = os.environ.get("VK_CLIENT_ID", "").strip()
    sec = os.environ.get("VK_CLIENT_SECRET", "").strip()
    if cid and sec:
        out.append((cid, sec))
    n = 2
    while True:
        cid = os.environ.get(f"VK_CLIENT_ID_{n}", "").strip()
        sec = os.environ.get(f"VK_CLIENT_SECRET_{n}", "").strip()
        if not cid or not sec:
            break
        out.append((cid, sec))
        n += 1
    return out


def _cabinet_login(token: str) -> str:
    """Login аккаунта — стабильная метка кабинета в таблице (человекочитаемые имена — в дашборде)."""
    try:
        u = _api_get(token, "user.json")
        return str(u.get("username") or u.get("id") or "").strip()
    except Exception:
        return ""


def _fetch_cabinet_rows(token: str, cabinet: str, df: str, dt: str, plans: dict) -> list:
    ids = list(plans.keys())
    print(f"[lime_vk_ads] {cabinet}: кампаний (ad_plans) {len(ids)}")
    base_map, goals_map = {}, {}
    for batch in _chunked(ids, STAT_BATCH):
        csv = ",".join(batch)
        base_map.update(parse_base_stats(
            _api_get(token, f"statistics/ad_plans/day.json?id={csv}&date_from={df}&date_to={dt}")))
        time.sleep(2)  # rate-limit
        goals_map.update(parse_goal_stats(
            _api_get(token, f"statistics/goals/ad_plans/day.json?id={csv}&date_from={df}&date_to={dt}")))
        time.sleep(2)
    return build_rows(base_map, goals_map, plans, cabinet=cabinet)


def sync_lime_vk_ads(days_back: int = 14) -> int:
    cabinets = _cabinets()
    if not cabinets:
        raise RuntimeError("VK_CLIENT_ID / VK_CLIENT_SECRET не заданы")

    to = date.today()
    frm = to - timedelta(days=days_back)
    df, dt = frm.isoformat(), to.isoformat()

    all_rows: list = []
    entity_rows: list = []
    for client_id, secret in cabinets:
        token = _get_token(client_id, secret)
        cabinet = _cabinet_login(token)
        plans = fetch_ad_plans(token)
        all_rows.extend(_fetch_cabinet_rows(token, cabinet, df, dt, plans))
        # Справочник группа→кампания собираем на ТОМ ЖЕ токене: VK лимитирует число активных
        # токенов на client_id, а при token_limit_exceeded код отзывает все токены клиента.
        entity_rows.extend(fetch_ad_groups(token, cabinet))
        entity_rows.extend(self_plan_rows(plans.keys(), cabinet))

    # ad_plan_id глобально уникальны между кабинетами (0 пересечений) → PK (date,campaign_id) без cabinet.
    n = _upsert(all_rows, df, dt)
    e = _upsert_entities(entity_rows)
    print(f"[lime_vk_ads] {n} строк из {len(cabinets)} кабинетов ({df}..{dt}); "
          f"справочник lime_vk_entities: {e} строк")
    return n


if __name__ == "__main__":
    sync_lime_vk_ads(days_back=int(os.environ.get("LIME_VK_DAYS_BACK", "14")))
