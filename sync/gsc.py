# -*- coding: utf-8 -*-
"""Google Search Console API → lime_gsc_seo (регионы KZ и GCC).

Недельные брендовые показы и клики Google. Спрос = показы, SEO = клики (в KZ и GCC
Google доминирует). ОТДЕЛЬНО от Яндекс.Вебмастера (lime_brand_seo, RU): другая выдача,
другой регион — не суммировать.

Страна берётся из dimension country (гео ПОЛЬЗОВАТЕЛЯ), не из домена: пользователи
Бахрейна видят в выдаче в основном ae./sa., по своему домену их спрос почти не виден.
Показы и клики суммируются по всем ресурсам региона — ряд «Спрос» это показы наших
доменов в выдаче, а не число поисков бренда (см. дизайн-спеку).

Контракт searchanalytics.query: rows[].{keys:[date, query], clicks, impressions, ctr,
position} (подтверждён зондом). Фильтр — одна группа (AND): query по регексу написаний
бренда И country=<ISO alpha-3>.

Auth: сервис-аккаунт добавлен пользователем ресурсов в Search Console (siteFullUser на
всех семи). Env: GOOGLE_APPLICATION_CREDENTIALS | GOOGLE_SERVICE_ACCOUNT, DATABASE_URL.

Запуск: python -m sync.gsc  (или из sync_brand.py).
"""
import datetime as dt
import json
import os

from sync.brand_terms import brand_regex, is_brand_query

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
ROW_LIMIT = 25000

# Ресурсы и страны по регионам. Значение в countries — то, что ложится в колонку
# lime_gsc_seo.country: русское название как в lime_stats (sync/gcc_channels.py),
# для KZ — пустая строка (регион целиком, без разбивки).
REGIONS = {
    "kz": {
        # KZ Google-SEO живёт на limestore.com (/kz_ru); lime-shop.com — RU-хост Вебмастера.
        "sites": ["https://limestore.com/"],
        "countries": {"kaz": ""},
    },
    "gcc": {
        # Только 6 страновых витрин Залива. Корневой limestore.com сознательно НЕ входит
        # (решение Павла, 2026-07-18): он тоже собирает брендовый спрос Залива, но клики с
        # него ведут на глобальный сайт, а не в магазин, и по малым странам он перекрывал
        # картину — при его включении у Бахрейна 75% органических кликов приходилось на
        # него, у Кувейта 67%. Блок отвечает на вопрос «как работает магазин Залива»,
        # поэтому считаем по его витринам. Цифры сравнения — в дизайн-спеке.
        "sites": [
            "https://ae.limestore.com/",
            "https://sa.limestore.com/",
            "https://kw.limestore.com/",
            "https://qa.limestore.com/",
            "https://bh.limestore.com/",
            "https://om.limestore.com/",
        ],
        "countries": {
            "are": "ОАЭ",
            "sau": "Саудовская Аравия",
            "kwt": "Кувейт",
            "qat": "Катар",
            "bhr": "Бахрейн",
            "omn": "Оман",
        },
    },
}


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


def _monday(date_str: str) -> str:
    d = dt.date.fromisoformat(date_str[:10])
    return (d - dt.timedelta(days=d.weekday())).isoformat()


def aggregate_by_country(rows: list[dict], region: str) -> dict[tuple, dict]:
    """[{query,date,clicks,impressions,country}] → {(week_start, country): {clicks, impressions}}.

    Строки разных ресурсов по одной стране СУММИРУЮТСЯ. Дедупа нет намеренно: запрос к API
    один на (ресурс, страна), пересечений между ними не бывает, а прежний дедуп по
    (date, query) при семи ресурсах терял данные.
    """
    out: dict[tuple, dict] = {}
    for r in rows:
        if not is_brand_query(r.get("query", ""), region):
            continue
        key = (_monday(r["date"]), r.get("country", ""))
        acc = out.setdefault(key, {"clicks": 0, "impressions": 0})
        acc["clicks"] += int(r.get("clicks", 0) or 0)
        acc["impressions"] += int(r.get("impressions", 0) or 0)
    return out


def fetch_site_country(service, site: str, country: str, region: str,
                       start: str, end: str) -> list[dict]:
    """Брендовые запросы ресурса из страны за период → [{query,date,clicks,impressions}].

    Один запрос с регекспом написаний бренда (RE2) — раньше был запрос на каждый термин
    с дедупом, теперь он не нужен. Пагинация по startRow.
    """
    rows: list[dict] = []
    start_row = 0
    while True:
        body = {
            "startDate": start,
            "endDate": end,
            "dimensions": ["date", "query"],
            "dimensionFilterGroups": [
                {"filters": [
                    {"dimension": "query", "operator": "includingRegex",
                     "expression": brand_regex(region)},
                    {"dimension": "country", "operator": "equals", "expression": country},
                ]}
            ],
            "rowLimit": ROW_LIMIT,
            "startRow": start_row,
            "type": "web",
        }
        resp = service.searchanalytics().query(siteUrl=site, body=body).execute()
        batch = parse_search_analytics(resp)
        rows += batch
        if len(batch) < ROW_LIMIT:
            break
        start_row += ROW_LIMIT
    return rows


def sync_gsc_seo(from_date: str, to_date: str, region: str = "kz") -> int:
    """Синк недельных брендовых показов/кликов Google по региону. Число строк (неделя×страна)."""
    cfg = REGIONS[region]
    service = get_searchconsole_service()
    have = accessible_sites(service)

    all_rows: list[dict] = []
    for site in cfg["sites"]:
        if site not in have:
            print(f"gsc[{region}]: пропуск {site} — нет доступа сервис-аккаунта")
            continue
        for iso_country, country_name in cfg["countries"].items():
            batch = fetch_site_country(service, site, iso_country, region, from_date, to_date)
            for r in batch:
                r["country"] = country_name
            all_rows += batch

    weekly = aggregate_by_country(all_rows, region)
    if not weekly:
        return 0
    from sync.db import get_connection  # ленивый импорт psycopg2

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO lime_gsc_seo (week_start, region, country, clicks, impressions, updated_at)
                VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (week_start, region, country)
                DO UPDATE SET clicks = EXCLUDED.clicks, impressions = EXCLUDED.impressions,
                              updated_at = now()
                """,
                [(wk, region, country, v["clicks"], v["impressions"])
                 for (wk, country), v in sorted(weekly.items())],
            )
        conn.commit()
    return len(weekly)


if __name__ == "__main__":
    frm = os.environ.get("GSC_FROM") or (dt.date.today() - dt.timedelta(weeks=8)).isoformat()
    today = dt.date.today().isoformat()
    for reg in ("kz", "gcc"):
        print(f"gsc[{reg}]:", sync_gsc_seo(frm, today, reg), "строк (неделя×страна)")
