"""Яндекс Вебмастер API → lime_brand_seo.

Недельные брендовые SEO-клики (Σ бренд-запросов по обоим хостам).
Контракт query-analytics/list: text_indicator_to_statistics[].{text_indicator.value,
statistics[].{date, field(CLICKS/IMPRESSIONS/CTR/POSITION/DEMAND), value}}.
Серверный бренд-фильтр: filters.text_filters TEXT_CONTAINS по написаниям из brand_terms.

Env: WORDSTAT_WEBMASTER_TOKEN, DATABASE_URL.
"""
import datetime as dt
import os

import requests

from sync.brand_terms import is_brand_query, terms_for

WM_BASE = "https://api.webmaster.yandex.net/v4"
USER_ID = "1343007866"
HOSTS = ["https:limestore.com:443", "https:lime-shop.com:443"]
# Написания бренда — из sync/brand_terms.py (общий источник с GSC-синком).
BRAND_TERMS = terms_for("ru")
PAGE_LIMIT = 500


def _monday(date_str: str) -> str:
    d = dt.date.fromisoformat(date_str[:10])
    return (d - dt.timedelta(days=d.weekday())).isoformat()


def parse_query_analytics(data: dict) -> dict[str, list[dict]]:
    """Ответ query-analytics/list → {query: [{date, clicks, impressions}]}.

    Из statistics берём поля CLICKS и IMPRESSIONS, группируя по дате.
    """
    out: dict[str, list[dict]] = {}
    for item in data.get("text_indicator_to_statistics", []):
        query = item.get("text_indicator", {}).get("value", "")
        by_date: dict[str, dict] = {}
        for st in item.get("statistics", []):
            field, date, value = st.get("field"), st.get("date"), st.get("value")
            if not date or field not in ("CLICKS", "IMPRESSIONS"):
                continue
            rec = by_date.setdefault(date[:10], {"date": date[:10], "clicks": 0, "impressions": 0})
            rec["clicks" if field == "CLICKS" else "impressions"] = int(value or 0)
        out[query] = list(by_date.values())
    return out


def aggregate_seo_weekly(rows: list[dict], region: str = "ru") -> dict[str, dict]:
    """[{query,date,clicks,impressions}] → {week_start: {clicks, impressions}} (только бренд)."""
    out: dict[str, dict] = {}
    for r in rows:
        if not is_brand_query(r.get("query", ""), region):
            continue
        wk = _monday(r["date"])
        acc = out.setdefault(wk, {"clicks": 0, "impressions": 0})
        acc["clicks"] += int(r.get("clicks", 0) or 0)
        acc["impressions"] += int(r.get("impressions", 0) or 0)
    return out


def _post(path: str, body: dict) -> dict:
    token = os.environ["WORDSTAT_WEBMASTER_TOKEN"]
    r = requests.post(f"{WM_BASE}{path}", json=body, timeout=60,
                      headers={"Authorization": f"OAuth {token}", "Content-Type": "application/json"})
    r.raise_for_status()
    return r.json()


def fetch_host_brand(host_id: str) -> list[dict]:
    """Все бренд-запросы хоста (дедуп по тексту запроса) → [{query,date,clicks,impressions}].

    Два серверных фильтра (лайм/lime); дедуп по query, т.к. запрос может попасть в оба.
    Пагинация по offset до неполной страницы. Окно — дефолтное у API (последние недели).
    """
    by_query: dict[str, list[dict]] = {}
    for term in BRAND_TERMS:
        offset = 0
        while True:
            data = _post(
                f"/user/{USER_ID}/hosts/{host_id}/query-analytics/list",
                {
                    "limit": PAGE_LIMIT, "offset": offset,
                    "device_type_indicator": "ALL", "text_indicator": "QUERY",
                    "filters": {"text_filters": [
                        {"text_indicator": "QUERY", "operation": "TEXT_CONTAINS", "value": term}
                    ]},
                },
            )
            parsed = parse_query_analytics(data)
            by_query.update(parsed)  # дедуп: одна и та же query перезапишется теми же данными
            if len(data.get("text_indicator_to_statistics", [])) < PAGE_LIMIT:
                break
            offset += PAGE_LIMIT
    rows: list[dict] = []
    for query, day_rows in by_query.items():
        for dr in day_rows:
            rows.append({"query": query, **dr})
    return rows


def sync_brand_seo() -> int:
    """Синк недельных брендовых SEO-кликов (оба хоста суммируются). Число недель."""
    all_rows: list[dict] = []
    for host in HOSTS:
        all_rows += fetch_host_brand(host)
    weekly = aggregate_seo_weekly(all_rows)
    if not weekly:
        return 0
    from sync.db import get_connection  # ленивый импорт psycopg2

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO lime_brand_seo (week_start, clicks, impressions, source, updated_at)
                VALUES (%s, %s, %s, 'webmaster', now())
                ON CONFLICT (week_start)
                DO UPDATE SET clicks = EXCLUDED.clicks, impressions = EXCLUDED.impressions,
                              source = 'webmaster', updated_at = now()
                """,
                [(wk, v["clicks"], v["impressions"]) for wk, v in sorted(weekly.items())],
            )
        conn.commit()
    return len(weekly)