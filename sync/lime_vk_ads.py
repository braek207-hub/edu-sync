# -*- coding: utf-8 -*-
"""sync/lime_vk_ads.py — кабинет VK Реклама (ads.vk.com) → lime_vk_ads_stats.

Паритет с Директом: расход/клики/показы + конверсии по типам (jsonb). Валюта RUB —
конверсии валюты нет; spent как в кабинете (без НДС). Rate-limit строгий: батчи id,
паузы, ретрай 429, токен кэшируется на прогон.

ENV: DATABASE_URL, VK_CLIENT_ID, VK_CLIENT_SECRET, LIME_VK_DAYS_BACK (default 14).
Запуск: python -m sync.lime_vk_ads
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


def build_rows(base_map, goals_map, campaigns_meta) -> list:
    """Слить базу + конверсии + мету кампании в строки upsert. Ведёт база (только где есть статистика)."""
    rows = []
    for (date, cid), base in base_map.items():
        meta = campaigns_meta.get(cid, {})
        conv = goals_map.get((date, cid), {})
        rows.append({
            "date": date,
            "region": "ru",
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


def _get_token(client_id: str, secret: str) -> str:
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials", "client_id": client_id, "client_secret": secret,
    }).encode("utf-8")
    req = urllib.request.Request(TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8"))["access_token"]


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


def fetch_active_campaigns(token: str) -> dict:
    js = _api_get(token, "campaigns.json?limit=500&fields=id,name,status,objective&_status__in=active")
    return _campaigns_from_json(js)


_UPSERT_SQL = """
    INSERT INTO lime_vk_ads_stats
      (date, region, campaign_id, campaign_name, objective, status,
       shows, clicks, spent, goals_total, vk_result, conversions, updated_at)
    VALUES
      (%(date)s, %(region)s, %(campaign_id)s, %(campaign_name)s, %(objective)s, %(status)s,
       %(shows)s, %(clicks)s, %(spent)s, %(goals_total)s, %(vk_result)s, %(conversions)s::jsonb, NOW())
    ON CONFLICT (date, campaign_id) DO UPDATE SET
       region = EXCLUDED.region, campaign_name = EXCLUDED.campaign_name,
       objective = EXCLUDED.objective, status = EXCLUDED.status,
       shows = EXCLUDED.shows, clicks = EXCLUDED.clicks, spent = EXCLUDED.spent,
       goals_total = EXCLUDED.goals_total, vk_result = EXCLUDED.vk_result,
       conversions = EXCLUDED.conversions, updated_at = NOW()
"""


def _pg_url() -> str:
    return os.environ["DATABASE_URL"].split("?")[0]


def _upsert(rows: list) -> int:
    if not rows:
        return 0
    with psycopg2.connect(_pg_url(), connect_timeout=30) as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, _UPSERT_SQL, rows, page_size=500)
        conn.commit()
    return len(rows)


def _chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def sync_lime_vk_ads(days_back: int = 14) -> int:
    client_id = os.environ.get("VK_CLIENT_ID", "").strip()
    secret = os.environ.get("VK_CLIENT_SECRET", "").strip()
    if not client_id or not secret:
        raise RuntimeError("VK_CLIENT_ID / VK_CLIENT_SECRET не заданы")

    token = _get_token(client_id, secret)
    campaigns = fetch_active_campaigns(token)
    ids = list(campaigns.keys())
    print(f"[lime_vk_ads] активных кампаний: {len(ids)}")

    to = date.today()
    frm = to - timedelta(days=days_back)
    df, dt = frm.isoformat(), to.isoformat()

    base_map, goals_map = {}, {}
    for batch in _chunked(ids, STAT_BATCH):
        csv = ",".join(batch)
        base_map.update(parse_base_stats(
            _api_get(token, f"statistics/campaigns/day.json?id={csv}&date_from={df}&date_to={dt}")))
        time.sleep(2)  # rate-limit
        goals_map.update(parse_goal_stats(
            _api_get(token, f"statistics/goals/campaigns/day.json?id={csv}&date_from={df}&date_to={dt}")))
        time.sleep(2)

    rows = build_rows(base_map, goals_map, campaigns)
    n = _upsert(rows)
    print(f"[lime_vk_ads] upsert {n} строк в lime_vk_ads_stats ({df}..{dt})")
    return n


if __name__ == "__main__":
    sync_lime_vk_ads(days_back=int(os.environ.get("LIME_VK_DAYS_BACK", "14")))
