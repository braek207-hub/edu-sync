# -*- coding: utf-8 -*-
"""Google Search Console API → lime_gsc_seo (регионы KZ и GCC).

Недельные показы и клики Google. Спрос = показы, SEO = клики (в KZ и GCC
Google доминирует). ОТДЕЛЬНО от Яндекс.Вебмастера (lime_brand_seo, RU): другая выдача,
другой регион — не суммировать.

МЕТОДИКА = «качественный бренд»: бренд + анонимные = тотал − видимый небренд
(решение Павла 2026-08-21; замеры недели 33 — в докстринге fetch_site_totals):
- KZ: limestore.com, страна пользователя Казахстан (общий с RU хост — без гео там
  Россия, ~119 тыс кликов/нед). Небренда в KZ мало (34 клика, 3,5 тыс показов).
- GCC: каждая витрина, «страна» строки = страна витрины (ae → ОАЭ), пользователи
  любых стран. Небренд у ae — 53% показов при CTR 0,5% (категорийная выдача
  «tank top»/«blazer»/«linen pants» на поз. 2–12) — он и раздувал «спрос» до
  44,6 тыс показов при 2 тыс кликов; вычитается excludingRegex написаний бренда.
Анонимные (GSC прячет редкие запросы) остаются В ряду: по поведению это бренд
(CTR 5,4% против 0,5% у небренда — редкие длинные вариации написаний).
Прежние методики — в git-истории (бренд+гео до 20.08; тоталы листов 20–21.08)
и в отчёте panda-bi /reports/lime-brand-method-kz-gcc.

Корневой limestore.com в GCC не входит (решение 18.07): его клики ведут на глобальный
сайт, а не в магазин; для ОАЭ он мал (199 кликов/нед против 2 028 у витрины).

Контракт searchanalytics.query: rows[].{keys:[date], clicks, impressions} (dims=[date]).

Auth: сервис-аккаунт добавлен пользователем ресурсов в Search Console (siteFullUser на
всех семи). Env: GOOGLE_APPLICATION_CREDENTIALS | GOOGLE_SERVICE_ACCOUNT, DATABASE_URL.

Запуск: python -m sync.gsc  (или из sync_brand.py).
"""
import datetime as dt
import json
import os

from sync.brand_terms import brand_regex

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
ROW_LIMIT = 25000

# Ресурсы по регионам. sites: {siteUrl: страна-строки в lime_gsc_seo.country}.
# Для KZ страна строки пустая (регион целиком), но запрос фильтруется по гео
# пользователя (country_filter): limestore.com — общий с RU хост, без фильтра
# там Россия (~119 тыс брендовых кликов/нед против ~2 тыс казахстанских).
REGIONS = {
    "kz": {
        "sites": {"https://limestore.com/": ""},
        "country_filter": "kaz",
    },
    "gcc": {
        "sites": {
            "https://ae.limestore.com/": "ОАЭ",
            "https://sa.limestore.com/": "Саудовская Аравия",
            "https://kw.limestore.com/": "Кувейт",
            "https://qa.limestore.com/": "Катар",
            "https://bh.limestore.com/": "Бахрейн",
            "https://om.limestore.com/": "Оман",
        },
        "country_filter": None,
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


def parse_daily_totals(resp: dict) -> list[dict]:
    """rows[].{keys:[date], clicks, impressions} → [{date, clicks, impressions}]."""
    out: list[dict] = []
    for r in resp.get("rows", []):
        keys = r.get("keys", [])
        if not keys:
            continue
        out.append({
            "date": keys[0],
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


def aggregate_weekly(rows: list[dict]) -> dict[tuple, dict]:
    """[{date,clicks,impressions,country}] → {(week_start, country): {clicks, impressions}}.

    Дневные тоталы витрин суммируются в ISO-неделю; строки разных витрин одной страны
    не пересекаются (страна = витрина), дедуп не нужен.
    """
    out: dict[tuple, dict] = {}
    for r in rows:
        key = (_monday(r["date"]), r.get("country", ""))
        acc = out.setdefault(key, {"clicks": 0, "impressions": 0})
        acc["clicks"] += int(r.get("clicks", 0) or 0)
        acc["impressions"] += int(r.get("impressions", 0) or 0)
    return out


def _daily_query(service, site: str, start: str, end: str, filters: list[dict]) -> list[dict]:
    body = {
        "startDate": start,
        "endDate": end,
        "dimensions": ["date"],
        "rowLimit": ROW_LIMIT,
        "type": "web",
    }
    if filters:
        body["dimensionFilterGroups"] = [{"filters": filters}]
    resp = service.searchanalytics().query(siteUrl=site, body=body).execute()
    return parse_daily_totals(resp)


def subtract_days(total: list[dict], nonbrand: list[dict]) -> list[dict]:
    """«Качественный бренд» = тотал − видимый небренд, по дням.

    Отрицательные значения клампятся в 0: выборки total и nonbrand снимаются двумя
    запросами, и на дне с досчитывающейся статистикой разность может мигнуть ниже нуля.
    """
    nb = {r["date"]: r for r in nonbrand}
    out: list[dict] = []
    for r in total:
        n = nb.get(r["date"], {})
        out.append({
            "date": r["date"],
            "clicks": max(0, int(r.get("clicks", 0)) - int(n.get("clicks", 0) or 0)),
            "impressions": max(0, int(r.get("impressions", 0)) - int(n.get("impressions", 0) or 0)),
        })
    return out


def fetch_site_totals(service, site: str, country_filter: str | None,
                      start: str, end: str, region: str) -> list[dict]:
    """Дневной «качественный бренд» ресурса = тотал − видимый небренд →
    [{date,clicks,impressions}].

    Два запроса dims=[date] (решение Павла 2026-08-21, замер недели 33):
    - тотал без query-фильтра: бренд + анонимные + небренд (у ae небренд — 53%
      показов при CTR 0,5%: категорийная выдача «tank top»/«blazer» на поз. 2–12);
    - excludingRegex(написания бренда): ВИДИМЫЙ небренд (анонимные при любом
      query-фильтре из выборки выпадают, поэтому в разности они остаются).
    Разность = бренд + анонимные. Анонимные по поведению — бренд (CTR 5,4% против
    0,5% у небренда): редкие длинные вариации, которые GSC прячет.
    Замер ae нед.33: 44 614 − 23 807 = 20 807 показов, 2 038 − 113 = 1 925 кликов.
    country_filter — гео пользователя (только KZ: общий с RU хост).
    """
    country = ([{"dimension": "country", "operator": "equals", "expression": country_filter}]
               if country_filter else [])
    total = _daily_query(service, site, start, end, country)
    nonbrand = _daily_query(service, site, start, end, country + [
        {"dimension": "query", "operator": "excludingRegex", "expression": brand_regex(region)},
    ])
    return subtract_days(total, nonbrand)


def sync_gsc_seo(from_date: str, to_date: str, region: str = "kz") -> int:
    """Синк недельных показов/кликов Google по региону. Число строк (неделя×страна).

    from_date прижимается к понедельнику своей недели: инкремент «сегодня − 8 недель»
    попадал в середину недели, граничная неделя приходила без первых дней, и upsert
    перезаписывал её полное значение усечённой суммой — в пределе одним днём (порча
    недель 2026-05-18…06-22 в обоих регионах, обнаружена 2026-08-19)."""
    from_date = _monday(from_date)
    cfg = REGIONS[region]
    service = get_searchconsole_service()
    have = accessible_sites(service)

    all_rows: list[dict] = []
    for site, country_name in cfg["sites"].items():
        if site not in have:
            print(f"gsc[{region}]: пропуск {site} — нет доступа сервис-аккаунта")
            continue
        batch = fetch_site_totals(service, site, cfg["country_filter"], from_date, to_date, region)
        for r in batch:
            r["country"] = country_name
        all_rows += batch

    weekly = aggregate_weekly(all_rows)
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
