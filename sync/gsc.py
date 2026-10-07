# -*- coding: utf-8 -*-
"""Google Search Console API → lime_gsc_seo (регионы KZ и GCC).

Недельные показы и клики Google. Спрос = показы, SEO = клики (в KZ и GCC
Google доминирует). ОТДЕЛЬНО от Яндекс.Вебмастера (lime_brand_seo, RU): другая выдача,
другой регион — не суммировать.

МЕТОДИКА = «качественный бренд»: бренд + анонимные = тотал − видимый небренд
(решение Павла 2026-08-21; замеры недели 33 — в докстринге fetch_site_totals).
Небренд у ae — 53% показов при CTR 0,5% (категорийная выдача «tank top»/«blazer»),
вычитается excludingRegex написаний бренда. Анонимные (GSC прячет редкие запросы)
остаются В ряду: по поведению это бренд (CTR 6,8% против 0,5% у небренда, 07.09–04.10.2026).
- KZ: limestore.com, пользователи из Казахстана (общий с RU хост — без гео там Россия,
  ~119 тыс кликов/нед). Чужие витрины у казахстанцев — 7% ключей, дубли ≤1%: не берём.
- GCC (решение Павла 2026-10-07; пробы scripts/probe_gsc_cross_sites.py,
  probe_gsc_root_gcc.py): строка страны = её пользователи, а не витрина целиком.
  1. Витрина страны с фильтром «пользователь из этой страны». Без фильтра половина и
     больше показов витрин — глобальные поиски «lime» из США/UK/ЕС (у kw свои
     пользователи всего 7% показов) — не спрос Залива.
  2. Корневой limestore.com у тех же пользователей: КЛИКИ прибавляются целиком (клик
     уходит на один сайт, дублей нет; у ОАЭ ~1 тыс кликов/мес), ПОКАЗЫ — только сверх
     витрины. GSC засчитывает показ каждому URL-prefix ресурсу отдельно, а корень почти
     всегда стоит в той же выдаче ниже витрины (ОАЭ: поз. 1,3 против 4,3; из 21,7 тыс
     брендовых показов корня без витрины — 789). Сверх витрины = по ключам
     дата×запрос×устройство видимого бренда max(0, корень − витрина); анонимные показы
     корня сопоставить не с чем — не берём. В Бахрейне витрина мертва, бренд живёт на
     корне — формула отдаёт его сама.
  Идеал — доменный ресурс sc-domain:limestore.com (дедуплицирует хосты сам); у
  сервис-аккаунта его нет.
Прежние методики — в git-истории (бренд+гео до 20.08; тоталы листов 20–21.08; витрина
целиком без гео 21.08–07.10) и в отчёте panda-bi /reports/lime-brand-method-kz-gcc.

Контракт searchanalytics.query: rows[].{keys:[date], clicks, impressions} (dims=[date]);
для сверки с корнем — keys:[date, query, device].

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
# KZ: страна строки пустая (регион целиком), запрос с гео пользователя country_filter.
# GCC: codes — гео пользователя для витрины (ISO alpha-3, как отдаёт GSC); root —
# корневой ресурс, чьи клики и показы сверх витрины добавляются стране.
ROOT_SITE = "https://limestore.com/"
REGIONS = {
    "kz": {
        "sites": {ROOT_SITE: ""},
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
        "codes": {
            "https://ae.limestore.com/": "are",
            "https://sa.limestore.com/": "sau",
            "https://kw.limestore.com/": "kwt",
            "https://qa.limestore.com/": "qat",
            "https://bh.limestore.com/": "bhr",
            "https://om.limestore.com/": "omn",
        },
        "root": ROOT_SITE,
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


def aggregate_daily(rows: list[dict]) -> dict[tuple, dict]:
    """[{date,clicks,impressions,country}] → {(day, country): {clicks, impressions}}.

    Свёртка нужна, даже когда витрины страны не пересекаются: если однажды две витрины
    получат одну страну, executemany с ON CONFLICT записал бы ПОСЛЕДНЮЮ строку вместо
    суммы (внутри одного батча конфликт разрешается построчно).
    """
    out: dict[tuple, dict] = {}
    for r in rows:
        key = (r["date"], r.get("country", ""))
        acc = out.setdefault(key, {"clicks": 0, "impressions": 0})
        acc["clicks"] += int(r.get("clicks", 0) or 0)
        acc["impressions"] += int(r.get("impressions", 0) or 0)
    return out


def _query(service, site: str, start: str, end: str, dims: list[str],
           filters: list[dict]) -> list[dict]:
    body = {
        "startDate": start,
        "endDate": end,
        "dimensions": dims,
        "rowLimit": ROW_LIMIT,
        "type": "web",
    }
    if filters:
        body["dimensionFilterGroups"] = [{"filters": filters}]
    out: list[dict] = []
    while True:
        if out:
            body["startRow"] = len(out)
        rows = service.searchanalytics().query(siteUrl=site, body=body).execute().get("rows", [])
        out += rows
        if len(rows) < ROW_LIMIT:
            return out


def _daily_query(service, site: str, start: str, end: str, filters: list[dict]) -> list[dict]:
    return parse_daily_totals({"rows": _query(service, site, start, end, ["date"], filters)})


def _country(code: str | None) -> list[dict]:
    return [{"dimension": "country", "operator": "equals", "expression": code}] if code else []


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
    country_filter — гео пользователя (KZ: общий с RU хост; GCC: страна витрины).
    """
    country = _country(country_filter)
    total = _daily_query(service, site, start, end, country)
    nonbrand = _daily_query(service, site, start, end, country + [
        {"dimension": "query", "operator": "excludingRegex", "expression": brand_regex(region)},
    ])
    return subtract_days(total, nonbrand)


def root_extra_impressions(home_rows: list[dict], root_rows: list[dict]) -> dict[str, int]:
    """Показы корня сверх витрины по дням: Σ по ключам дата×запрос×устройство
    max(0, корень − витрина). Ключ, где в выдаче оба ресурса, — те же поиски,
    засчитанные дважды; превышение корня — поиски, где витрины не было."""
    home = {tuple(r["keys"]): int(r.get("impressions", 0) or 0) for r in home_rows}
    out: dict[str, int] = {}
    for r in root_rows:
        key = tuple(r["keys"])
        extra = int(r.get("impressions", 0) or 0) - home.get(key, 0)
        if extra > 0:
            out[key[0]] = out.get(key[0], 0) + extra
    return out


def fetch_gcc_country(service, site: str, code: str, root: str | None,
                      start: str, end: str) -> list[dict]:
    """Дневной ряд страны GCC = витрина у пользователей страны + корень у них же:
    клики целиком, показы — сверх витрины (см. докстринг модуля)."""
    days = {r["date"]: r for r in fetch_site_totals(service, site, code, start, end, "gcc")}
    if root:
        for r in fetch_site_totals(service, root, code, start, end, "gcc"):
            days.setdefault(r["date"], {"date": r["date"], "clicks": 0, "impressions": 0})
            days[r["date"]]["clicks"] += r["clicks"]
        brand = _country(code) + [
            {"dimension": "query", "operator": "includingRegex", "expression": brand_regex("gcc")},
        ]
        dims = ["date", "query", "device"]
        extra = root_extra_impressions(_query(service, site, start, end, dims, brand),
                                       _query(service, root, start, end, dims, brand))
        for day, n in extra.items():
            days.setdefault(day, {"date": day, "clicks": 0, "impressions": 0})
            days[day]["impressions"] += n
    return [days[d] for d in sorted(days)]


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
        if region == "gcc":
            root = cfg["root"] if cfg["root"] in have else None
            if root is None:
                print(f"gsc[{region}]: нет доступа к {cfg['root']} — {country_name} без корня")
            batch = fetch_gcc_country(service, site, cfg["codes"][site], root, from_date, to_date)
        else:
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
            # Дневной срез из ТЕХ ЖЕ строк — переключателю «Недели/Дни» на Google-вкладках.
            # Отдельных запросов к API не нужно: fetch_site_totals и так снимает дни,
            # неделя — их сумма. Пишем в одной транзакции с недельным, чтобы зерна не
            # разъезжались (иначе после падения между двумя upsert'ами день и неделя
            # показали бы разные числа за один и тот же период).
            cur.executemany(
                """
                INSERT INTO lime_gsc_seo_daily (day, region, country, clicks, impressions, updated_at)
                VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (day, region, country)
                DO UPDATE SET clicks = EXCLUDED.clicks, impressions = EXCLUDED.impressions,
                              updated_at = now()
                """,
                [(day, region, country, v["clicks"], v["impressions"])
                 for (day, country), v in sorted(aggregate_daily(all_rows).items())],
            )
        conn.commit()
    return len(weekly)


if __name__ == "__main__":
    frm = os.environ.get("GSC_FROM") or (dt.date.today() - dt.timedelta(weeks=8)).isoformat()
    today = dt.date.today().isoformat()
    for reg in ("kz", "gcc"):
        print(f"gsc[{reg}]:", sync_gsc_seo(frm, today, reg), "строк (неделя×страна)")
