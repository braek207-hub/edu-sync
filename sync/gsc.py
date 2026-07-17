# -*- coding: utf-8 -*-
"""Google Search Console API → lime_gsc_seo (регион KZ).

Недельные брендовые SEO-клики Google по КАЗАХСТАНУ (country=kaz). В KZ доминирует Google,
поэтому Google-блок бренд-трафика = KZ-регион (Trends спрос KZ + GSC SEO KZ + Google Ads KZ).
ОТДЕЛЬНО от Яндекс.Вебмастера (lime_brand_seo, RU): другая выдача, другой регион — не суммировать.
Недельная гранулярность (Пн ISO), region='kz'.

Контракт searchanalytics.query: rows[].{keys:[date, query], clicks, impressions, ctr, position}
(подтверждён живым зондом). Фильтр (в одной группе, AND): query содержит 'lime'/'лайм' И
country=kaz. Два термина — отдельными запросами (разные группы = AND, не OR), дедуп по (date,query).

Auth: сервис-аккаунт (тот же ключ, что Google Sheets) добавлен пользователем ресурса
limestore.com в Search Console. Env: GOOGLE_APPLICATION_CREDENTIALS | GOOGLE_SERVICE_ACCOUNT, DATABASE_URL.

Запуск: python -m sync.gsc  (или из sync_brand.py).
"""
import json
import os

from sync.webmaster import aggregate_seo_weekly  # brand-only weekly Σ (та же логика)

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
# KZ Google-SEO живёт на limestore.com (/kz_ru); lime-shop.com — RU-хост Яндекс.Вебмастера.
SITES = ["https://limestore.com/"]
BRAND_TERMS = ["lime", "лайм"]
KZ_COUNTRY = "kaz"  # ISO-3166-1 alpha-3 (country dimension в GSC)
REGION = "kz"
ROW_LIMIT = 25000


def get_searchconsole_service():
    """Клиент Search Console v1 из сервис-аккаунта (лениво — google-либы не нужны тестам)."""
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        creds = Credentials.from_service_account_file(
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"], scopes=SCOPES
        )
    else:
        creds = Credentials.from_service_account_info(
            json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT"]), scopes=SCOPES
        )
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def parse_search_analytics(resp: dict) -> list[dict]:
    """rows[].{keys:[date, query], clicks, impressions} → [{query, date, clicks, impressions}]."""
    out: list[dict] = []
    for r in resp.get("rows", []):
        keys = r.get("keys", [])
        if len(keys) < 2:
            continue
        out.append({
            "date": keys[0],
            "query": keys[1],
            "clicks": int(r.get("clicks", 0) or 0),
            "impressions": int(r.get("impressions", 0) or 0),
        })
    return out


def accessible_sites(service) -> set[str]:
    """siteUrl'ы, к которым у сервис-аккаунта есть доступ (sites.list)."""
    entries = service.sites().list().execute().get("siteEntry", [])
    return {e["siteUrl"] for e in entries}


def fetch_site_brand(service, site: str, start: str, end: str) -> list[dict]:
    """Бренд-запросы сайта из KZ за период → [{query,date,clicks,impressions}].

    Отдельный запрос на каждый термин (в GSC разные dimensionFilterGroups = AND, не OR),
    внутри группы (query содержит термин) AND (country=kaz). Дедуп по (date, query). Пагинация.
    """
    by_key: dict[tuple, dict] = {}
    for term in BRAND_TERMS:
        start_row = 0
        while True:
            body = {
                "startDate": start,
                "endDate": end,
                "dimensions": ["date", "query"],
                "dimensionFilterGroups": [
                    {"filters": [
                        {"dimension": "query", "operator": "contains", "expression": term},
                        {"dimension": "country", "operator": "equals", "expression": KZ_COUNTRY},
                    ]}
                ],
                "rowLimit": ROW_LIMIT,
                "startRow": start_row,
                "type": "web",
            }
            resp = service.searchanalytics().query(siteUrl=site, body=body).execute()
            batch = parse_search_analytics(resp)
            for r in batch:
                by_key[(r["date"], r["query"])] = r  # метрики (date,query) идентичны → дедуп
            if len(batch) < ROW_LIMIT:
                break
            start_row += ROW_LIMIT
    return list(by_key.values())


def sync_gsc_seo(from_date: str, to_date: str) -> int:
    """Синк недельных брендовых SEO-кликов Google по KZ (country=kaz). Число недель."""
    service = get_searchconsole_service()
    have = accessible_sites(service)
    all_rows: list[dict] = []
    for site in SITES:
        if site not in have:
            print(f"gsc: пропуск {site} — нет доступа сервис-аккаунта")
            continue
        all_rows += fetch_site_brand(service, site, from_date, to_date)
    weekly = aggregate_seo_weekly(all_rows)  # brand-only, ключ = ISO-понедельник
    if not weekly:
        return 0
    from sync.db import get_connection  # ленивый импорт psycopg2

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO lime_gsc_seo (week_start, region, clicks, impressions, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (week_start, region)
                DO UPDATE SET clicks = EXCLUDED.clicks, impressions = EXCLUDED.impressions,
                              updated_at = now()
                """,
                [(wk, REGION, v["clicks"], v["impressions"]) for wk, v in sorted(weekly.items())],
            )
        conn.commit()
    return len(weekly)


if __name__ == "__main__":
    import datetime as dt

    frm = os.environ.get("GSC_FROM") or (dt.date.today() - dt.timedelta(weeks=8)).isoformat()
    print("gsc:", sync_gsc_seo(frm, dt.date.today().isoformat()), "недель")
