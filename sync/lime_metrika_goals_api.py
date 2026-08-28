# -*- coding: utf-8 -*-
"""Яндекс.Метрика: справочник целей счётчика + достижения целей по каналу/кампании.

Состав целей НЕ зашит константами (как четыре цели в lime_ru_metrika_api): в счётчике
живут регистрация, авторизация, цели Mindbox и автоцели, и список меняется без нашего
участия. Синк спрашивает его у Management API каждым прогоном.

Stat API берёт ограниченное число метрик за запрос, поэтому цели тянутся пачками
(GOALS_PER_REQUEST) и склеиваются по ключу разреза.
"""
import os
import time

import requests

MANAGEMENT_URL = "https://api-metrika.yandex.net/management/v1/counter/{counter}/goals"
STAT_URL = "https://api-metrika.yandex.net/stat/v1/data"

RETRIES = int(os.environ.get("LIME_METRIKA_GOALS_RETRIES") or "3")
RETRY_SLEEP = int(os.environ.get("LIME_METRIKA_GOALS_RETRY_SLEEP") or "5")

# Stat API принимает до 20 метрик за запрос; берём с запасом, чтобы не упереться в лимит
# при добавлении служебной метрики.
GOALS_PER_REQUEST = int(os.environ.get("LIME_METRIKA_GOALS_PER_REQUEST") or "18")

# Разрез тот же, что у основного RU-среза (lime_ru_metrika_api.DIMENSIONS) минус utm_content:
# он в свёртку целей не входит, а лишние измерения множат строки ответа.
DIMENSIONS = (
    "ym:s:date",
    "ym:s:lastsignTrafficSource",
    "ym:s:lastsignSourceEngine",
    "ym:s:lastsignDirectClickOrderName",
    "ym:s:lastsignUTMCampaign",
)

GEO_FILTER = "ym:s:regionCountryName=='Russia'"

# Максимум строк в ответе Stat API; длиннее — дочитываем смещением.
PAGE_LIMIT = 100000


def _request(url: str, params: dict, token: str) -> dict:
    headers = {"Authorization": f"OAuth {token}"}
    resp = None
    for attempt in range(1, RETRIES + 1):
        resp = requests.get(url, headers=headers, params=params, timeout=120)
        if resp.status_code == 200:
            return resp.json()
        if attempt < RETRIES:
            # 429 — квота запросов Метрики: она отпускает по времени, а не по удаче,
            # поэтому ждём дольше с каждой попыткой, а не фиксированные RETRY_SLEEP.
            time.sleep(RETRY_SLEEP * attempt * (4 if resp.status_code == 429 else 1))
    # `if resp` НЕ работает: Response.__bool__ ложен при статусе ≥400 — то есть ровно
    # в том случае, ради которого пишется это сообщение, и код ошибки терялся.
    raise RuntimeError(
        f"Метрика {url}: HTTP {resp.status_code if resp is not None else '?'} "
        f"{(resp.text[:300] if resp is not None else '')}"
    )


def fetch_goal_catalog(counter_id, token: str) -> list[dict]:
    """Цели счётчика: [{'goal_id', 'name', 'type', 'source', 'is_retargeting'}].

    useDirect=true — вместе с целями, заведёнными из Директа (в том числе автоцели).
    source (`goal_source` в ответе, напр. 'auto' | 'user' | 'ecommerce') нужен, чтобы
    отделить автоцели: их десятки, и по умолчанию дашборд их прячет — иначе список
    целей тонет в автосборе.
    """
    data = _request(MANAGEMENT_URL.format(counter=counter_id), {"useDirect": "true"}, token)
    out = []
    for g in data.get("goals", []):
        gid = g.get("id")
        if gid is None:
            continue
        out.append({
            "goal_id": str(gid),
            "name": (g.get("name") or "").strip(),
            "type": (g.get("type") or "").strip(),
            "source": (g.get("goal_source") or "").strip(),
            "is_retargeting": bool(g.get("is_retargeting")),
        })
    return out


def chunk_goals(goal_ids: list[str], size: int = GOALS_PER_REQUEST) -> list[list[str]]:
    return [goal_ids[i:i + size] for i in range(0, len(goal_ids), size)]


def parse_goal_rows(resp: dict, goal_ids: list[str]) -> list[dict]:
    """Ответ Stat API → плоские строки {разрез + goal_id + reaches}.

    Позиции измерений читаются из эха query, позиции метрик — из порядка goal_ids,
    в котором они были запрошены. Нули не отбрасываем здесь: это делает вызывающий,
    чтобы решение «хранить ли ноль» было в одном месте.
    """
    queried = (resp.get("query") or {}).get("dimensions") or []
    pos = {name: i for i, name in enumerate(queried)}

    def dim(dims: list, attr: str, field: str):
        i = pos.get(attr)
        if i is None or i >= len(dims):
            return None
        return (dims[i] or {}).get(field)

    rows: list[dict] = []
    for item in resp.get("data", []):
        dims = item.get("dimensions", [])
        metrics = item.get("metrics", []) or []
        base = {
            "date": dim(dims, "ym:s:date", "name"),
            "traffic_source": dim(dims, "ym:s:lastsignTrafficSource", "id"),
            "source_engine": dim(dims, "ym:s:lastsignSourceEngine", "name"),
            "direct_campaign_name": dim(dims, "ym:s:lastsignDirectClickOrderName", "name"),
            "utm_campaign": dim(dims, "ym:s:lastsignUTMCampaign", "name"),
        }
        for i, goal_id in enumerate(goal_ids):
            value = float(metrics[i] or 0) if i < len(metrics) else 0.0
            rows.append({**base, "goal_id": goal_id, "reaches": value})
    return rows


def fetch_goal_reaches(counter_id, token: str, date_from: str, date_to: str,
                       goal_ids: list[str]) -> list[dict]:
    """Достижения целей за ВЕСЬ период по каналу/кампании. Цели тянутся пачками.

    Период берётся одним запросом на пачку, а не днём за днём: ym:s:date — измерение,
    и день в ответе приходит своей строкой. Посуточный цикл давал (целей / 18) × дней
    запросов — на 110 целях и месяце это 210 обращений, и Метрика обрывала прогон
    квотой на середине. Здесь их 7.

    Ответ длиннее PAGE_LIMIT дочитывается смещением: total_rows Метрика возвращает сама.
    """
    out: list[dict] = []
    for chunk in chunk_goals(goal_ids):
        offset = 1
        while True:
            params = {
                "ids": counter_id,
                "date1": date_from,
                "date2": date_to,
                "metrics": ",".join(f"ym:s:goal{gid}reaches" for gid in chunk),
                "dimensions": ",".join(DIMENSIONS),
                "filters": GEO_FILTER,
                "accuracy": "full",
                "limit": PAGE_LIMIT,
                "offset": offset,
            }
            resp = _request(STAT_URL, params, token)
            page = resp.get("data") or []
            out.extend(parse_goal_rows(resp, chunk))
            offset += len(page)
            # total_rows — сколько строк у запроса всего; без явной остановки по нему
            # хвост периода молча терялся бы, а данные выглядели бы целыми.
            if not page or offset > int(resp.get("total_rows") or 0):
                break
    return out
