"""Ридер-оркестратор per-visit сессий Метрики через Logs API (Фаза B, Task 3).

Аналог `sync/edu_visits.py` (дневной агрегат Reporting API), но на уровне визита и
через `sync/logs_api.py` (Task 2): create → wait_processed → download parts → parse_tsv.
Точное `ym:s:dateTime` снимает потолок same-day-конвертеров, недоступный дневному агрегату.

Отобранный набор полей и анти-утечка (НЕ брать ym:s:goalsID/params — оплаты грузятся
нами как офлайн-цели) — см. Global Constraints плана
docs/superpowers/plans/2026-07-27-edu-ml-phase-b-logs-api.md. Фильтр client_id — как
edu_visits.py: пишем только визиты лидов (`load_lead_client_ids()`), не весь трафик счётчика.
"""

import os
from datetime import date, timedelta
from typing import Any, Dict, List, Set, Tuple

from sync.db import load_lead_client_ids, upsert_edu_visit_sessions
from sync.logs_api import bucket_topn, clean_request, create_request, download_part, parse_tsv, wait_processed
from sync.metrika_offline import COUNTER_VUZ

# Отобранные поля (Global Constraints плана, проба logs_api_fields_probe.py — 46 валидных,
# здесь curated подмножество без goalsID/params). Порядок не важен для map_row (маппинг по имени
# в header), важен только состав.
FIELDS = ",".join(
    [
        "ym:s:dateTime",
        "ym:s:clientID",
        "ym:s:visitID",
        "ym:s:visitDuration",
        "ym:s:bounce",
        "ym:s:pageViews",
        "ym:s:isNewUser",
        "ym:s:counterUserIDHash",
        "ym:s:UTMSource",
        "ym:s:UTMMedium",
        "ym:s:UTMCampaign",
        "ym:s:UTMContent",
        "ym:s:UTMTerm",
        "ym:s:firstTrafficSource",
        "ym:s:lastsignTrafficSource",
        "ym:s:lastSourceEngine",
        "ym:s:referer",
        "ym:s:lastDirectPlatformType",
        "ym:s:lastDirectConditionType",
        "ym:s:lastDirectPhraseOrCond",
        "ym:s:lastDirectClickOrderName",
        "ym:s:hasGCLID",
        "ym:s:deviceCategory",
        "ym:s:operatingSystem",
        "ym:s:browser",
        "ym:s:mobilePhoneModel",
        "ym:s:screenWidth",
        "ym:s:screenHeight",
        "ym:s:networkType",
    ]
)

# Высококардинальные поля → bucket_topn(топ-N + "other"), иначе взорвут DictVectorizer
# логистики one-hot'ом (Global Constraints плана). Ключ словаря = имя bucket'а в map_row/allowed_buckets.
_BUCKET_FIELDS = {
    "direct_phrase": "ym:s:lastDirectPhraseOrCond",
    "referer": "ym:s:referer",
    "utm_campaign": "ym:s:UTMCampaign",
    "phone_model": "ym:s:mobilePhoneModel",
}
_TOP_N = 20

# Простые (не bucket) текстовые поля — 1-в-1 в DB-колонку, пустая строка → None.
_PLAIN_FIELDS = {
    "user_id_hash": "ym:s:counterUserIDHash",
    "utm_source": "ym:s:UTMSource",
    "utm_medium": "ym:s:UTMMedium",
    "utm_content": "ym:s:UTMContent",
    "utm_term": "ym:s:UTMTerm",
    "first_traffic_source": "ym:s:firstTrafficSource",
    "lastsign_traffic_source": "ym:s:lastsignTrafficSource",
    "source_engine": "ym:s:lastSourceEngine",
    "direct_platform_type": "ym:s:lastDirectPlatformType",
    "direct_condition_type": "ym:s:lastDirectConditionType",
    "direct_order_name": "ym:s:lastDirectClickOrderName",
    "device_category": "ym:s:deviceCategory",
    "os": "ym:s:operatingSystem",
    "browser": "ym:s:browser",
    "network_type": "ym:s:networkType",
}

# Целочисленные поля — пустая строка (нет значения) → None, иначе int(float(...)) (Метрика
# отдаёт флаги как "0"/"1", числовые счётчики как целые строки).
_INT_FIELDS = {
    "visit_duration": "ym:s:visitDuration",
    "bounce": "ym:s:bounce",
    "page_views": "ym:s:pageViews",
    "is_new_user": "ym:s:isNewUser",
    "has_gclid": "ym:s:hasGCLID",
    "screen_w": "ym:s:screenWidth",
    "screen_h": "ym:s:screenHeight",
}


def _token() -> str:
    return os.environ.get("YM_TOKEN", "").strip()


def map_row(header: List[str], cols: List[str], allowed_buckets: Dict[str, Set[str]]) -> Dict[str, Any]:
    """Одна per-visit строка Logs API → dict под upsert_edu_visit_sessions.

    Только отобранные поля (НЕ ym:s:goalsID/params — анти-утечка); высококард. поля
    (direct_phrase/referer/utm_campaign/phone_model) — через bucket_topn(allowed_buckets[...]).
    Пропущенные в header поля (частичный header, напр. в тестах) → None, не падаем.
    """
    idx = {name: i for i, name in enumerate(header)}

    def get(field: str) -> str:
        i = idx.get(field)
        if i is None or i >= len(cols):
            return ""
        return (cols[i] or "").strip()

    def get_int(field: str):
        v = get(field)
        if not v:
            return None
        try:
            return int(float(v))
        except ValueError:
            return None

    def get_bucket(field: str, bucket_key: str) -> str:
        return bucket_topn(get(field), allowed_buckets.get(bucket_key, set()))

    row: Dict[str, Any] = {
        "visit_ts": get("ym:s:dateTime"),
        "client_id": get("ym:s:clientID"),
        "visit_id": get("ym:s:visitID"),
    }
    for db_col, field in _PLAIN_FIELDS.items():
        row[db_col] = get(field) or None
    for db_col, field in _INT_FIELDS.items():
        row[db_col] = get_int(field)
    for bucket_key, field in _BUCKET_FIELDS.items():
        row[bucket_key] = get_bucket(field, bucket_key)
    return row


def _build_allowed_buckets(header: List[str], rows: List[List[str]]) -> Dict[str, Set[str]]:
    """Топ-N значений высококард. полей на первом чанке → allow-множество для bucket_topn
    на всех последующих чанках (Global Constraints: бакетировать, не сырой one-hot)."""
    idx = {name: i for i, name in enumerate(header)}
    out: Dict[str, Set[str]] = {}
    for bucket_key, field in _BUCKET_FIELDS.items():
        i = idx.get(field)
        counts: Dict[str, int] = {}
        if i is not None:
            for cols in rows:
                if i >= len(cols):
                    continue
                v = (cols[i] or "").strip()
                if not v:
                    continue
                counts[v] = counts.get(v, 0) + 1
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:_TOP_N]
        out[bucket_key] = {v for v, _ in top}
    return out


def _chunk_windows(start: date, end_exclusive_today: date, chunk_days: int) -> List[Tuple[str, str]]:
    """Список (date1,date2)-окон бэкфилла (ISO-строки), чанк по `chunk_days`.

    Logs API отдаёт визиты только за дни строго ДО сегодня (400 "date2 must be before
    today" иначе) — верхняя граница каждого окна клампится на
    `end_exclusive_today - 1 день`. Если после клампа окно пустое (`date1 > date2`,
    случай когда исходный чанк — это только сегодня) — окно не включается в список
    вовсе (вызывающий код не создаёт logrequest на пустое окно)."""
    last_allowed = end_exclusive_today - timedelta(days=1)
    windows: List[Tuple[str, str]] = []
    cur = start
    while cur <= end_exclusive_today:
        c_from = cur
        c_to = min(cur + timedelta(days=chunk_days - 1), end_exclusive_today)
        cur = c_to + timedelta(days=1)
        c_to_clamped = min(c_to, last_allowed)
        if c_from <= c_to_clamped:
            windows.append((c_from.isoformat(), c_to_clamped.isoformat()))
    return windows


def _fetch_chunk(date1: str, date2: str, token: str) -> Tuple[List[str], List[List[str]]]:
    """create → wait_processed → скачать все части → склеить (header одинаков во всех
    частях одного request_id). clean_request — всегда, даже при ошибке скачивания/парсинга."""
    req_id = create_request(COUNTER_VUZ, date1, date2, FIELDS, token)
    try:
        parts = wait_processed(COUNTER_VUZ, req_id, token)
        header: List[str] = []
        rows: List[List[str]] = []
        for part in parts:
            tsv = download_part(COUNTER_VUZ, req_id, part, token)
            h, r = parse_tsv(tsv)
            if not header:
                header = h
            rows.extend(r)
        return header, rows
    finally:
        try:
            clean_request(COUNTER_VUZ, req_id, token)
        except Exception as e:
            print(f"  (clean_request не удался для request_id={req_id}: {e})")


def sync_edu_visit_logs(days_back: int = 90, chunk_days: int = 7) -> int:
    token = _token()
    if not token:
        print("EDU visits logs: YM_TOKEN не задан — пропуск")
        return 0
    keep = load_lead_client_ids()
    if not keep:
        print("EDU visits logs: нет client_id в лидах — пропуск")
        return 0

    today = date.today()
    start = today - timedelta(days=days_back)
    windows = _chunk_windows(start, today, chunk_days)
    print(
        f"EDU visits logs (vuz {COUNTER_VUZ}): {start} — {today - timedelta(days=1)}, "
        f"лидов-client_id={len(keep)}, чанк={chunk_days}д, окон={len(windows)}"
    )

    allowed_buckets: Dict[str, Set[str]] = {k: set() for k in _BUCKET_FIELDS}
    buckets_built = False

    total = 0
    for date1, date2 in windows:
        header, raw_rows = _fetch_chunk(date1, date2, token)

        if not buckets_built and raw_rows:
            allowed_buckets = _build_allowed_buckets(header, raw_rows)
            buckets_built = True

        mapped: List[Dict[str, Any]] = []
        client_idx = header.index("ym:s:clientID") if "ym:s:clientID" in header else None
        if client_idx is not None:
            for cols in raw_rows:
                if client_idx >= len(cols) or cols[client_idx] not in keep:
                    continue
                row = map_row(header, cols, allowed_buckets)
                row["counter_id"] = int(COUNTER_VUZ)
                mapped.append(row)

        n = upsert_edu_visit_sessions(mapped) if mapped else 0
        print(f"  {date1} — {date2}: строк всего={len(raw_rows)}, наших={len(mapped)}, upsert={n}")
        total += n
    return total


if __name__ == "__main__":
    import sys

    from dotenv import load_dotenv

    load_dotenv()
    days = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("EDU_VISITS_LOGS_DAYS", "90"))
    n = sync_edu_visit_logs(days)
    print(f"EDU visits logs: upsert {n} строк")
