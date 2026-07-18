# -*- coding: utf-8 -*-
"""Зонд GSC для региона GCC: есть ли брендовый спрос/SEO по странам Залива.

Ничего не пишет в БД. Запуск: python scripts/probe_gcc_gsc.py

Проверяет:
  P1 — какие ресурсы доступны сервис-аккаунту (sites.list). URL-prefix
       https://limestore.com/ НЕ включает поддомены ae./sa./... — важно для GCC.
  P2 — по каждой стране Залива (ISO alpha-3): clicks/impressions за 8 недель,
       фильтр по бренд-терминам (как в sync/gsc.py) и БЕЗ фильтра (весь сайт).
  P3 — топ-20 запросов по стране без фильтра термина → какие бренд-варианты
       реально есть (латиница / арабица) для BRAND_TERMS GCC.
"""
import datetime as dt
import json
import os
import pathlib

from dotenv import load_dotenv

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
GULF = {"are": "ОАЭ", "sau": "Саудовская Аравия", "kwt": "Кувейт",
        "qat": "Катар", "bhr": "Бахрейн", "omn": "Оман"}
BRAND_TERMS = ["lime", "лайм"]
FIX = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def service():
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        creds = Credentials.from_service_account_file(
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"], scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_info(
            json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT"]), scopes=SCOPES)
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def query(svc, site, start, end, country, term=None, dims=("date",), limit=25000):
    filters = [{"dimension": "country", "operator": "equals", "expression": country}]
    if term:
        filters.insert(0, {"dimension": "query", "operator": "contains", "expression": term})
    body = {"startDate": start, "endDate": end, "dimensions": list(dims),
            "dimensionFilterGroups": [{"filters": filters}], "rowLimit": limit, "type": "web"}
    return svc.searchanalytics().query(siteUrl=site, body=body).execute().get("rows", [])


def totals(rows):
    return (sum(int(r.get("clicks", 0)) for r in rows),
            sum(int(r.get("impressions", 0)) for r in rows))


def main():
    svc = service()
    end = dt.date.today() - dt.timedelta(days=3)   # GSC лаг ~2-3 дня
    start = end - dt.timedelta(weeks=8)
    s, e = start.isoformat(), end.isoformat()

    print("== P1: доступные ресурсы (sites.list) ==")
    sites = svc.sites().list().execute().get("siteEntry", [])
    for x in sites:
        print(f"  {x['siteUrl']}  [{x.get('permissionLevel')}]")
    urls = [x["siteUrl"] for x in sites]
    if not urls:
        print("  ПУСТО — сервис-аккаунт не добавлен ни в один ресурс")
        return

    print(f"\n== P2: страны Залива, {s} .. {e} ==")
    found = {}
    for site in urls:
        print(f"\n-- {site}")
        for cc, name in GULF.items():
            try:
                all_rows = query(svc, site, s, e, cc)
                brand = []
                for t in BRAND_TERMS:
                    brand += query(svc, site, s, e, cc, term=t)
                ac, ai = totals(all_rows)
                bc, bi = totals(brand)
                print(f"  {cc} {name:22} всего: {ac:>7} кликов / {ai:>8} показов "
                      f"| бренд: {bc:>6} / {bi:>7}")
                if ai:
                    found.setdefault(site, []).append(cc)
            except Exception as ex:  # noqa: BLE001 — зонд: печатаем и идём дальше
                print(f"  {cc} {name:22} ОШИБКА: {type(ex).__name__}: {str(ex)[:160]}")

    print("\n== P3: топ-20 запросов по стране (без фильтра термина) ==")
    for site, ccs in found.items():
        for cc in ccs:
            rows = query(svc, site, s, e, cc, dims=("query",), limit=20)
            rows.sort(key=lambda r: -int(r.get("impressions", 0)))
            print(f"\n-- {site} / {cc} ({GULF[cc]})")
            for r in rows[:20]:
                print(f"   {r['keys'][0][:48]:50} {int(r.get('clicks',0)):>6} кл / "
                      f"{int(r.get('impressions',0)):>7} пок")

    if found:
        FIX.mkdir(parents=True, exist_ok=True)
        site, ccs = next(iter(found.items()))
        sample = query(svc, site, s, e, ccs[0], dims=("date", "query"), limit=50)
        (FIX / "gsc_gcc_sample.json").write_text(
            json.dumps({"site": site, "country": ccs[0], "rows": sample},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nфикстура: tests/fixtures/gsc_gcc_sample.json ({len(sample)} строк)")


if __name__ == "__main__":
    main()
