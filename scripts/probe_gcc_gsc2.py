# -*- coding: utf-8 -*-
"""Зонд GSC для GCC, раунд 2: дырки, найденные при ревью дизайна. В БД не пишет.

Запуск: python scripts/probe_gcc_gsc2.py

P4 — корневой ресурс limestore.com даёт +70% бренд-показов по ОАЭ поверх ae.
     Кого он обслуживает: язык топ-запросов (кириллица = русскоязычные экспаты,
     их спрос к GCC-магазину не относится; латиница/арабица = наш GCC-спрос).
P5 — арабские бренд-варианты: ловит ли фильтр 'لايم' весь арабский бренд-спрос
     (сравнение с regex-фильтром по арабскому блоку Unicode).
P6 — задвоение при суммировании 6 поддоменов на одну страну: Σ(6 доменов) vs
     «домашний» домен страны. Насколько cross-domain показы раздувают спрос.
P7 — глубина истории GSC (16 мес): есть ли данные на ae. в апреле 2025.
"""
import datetime as dt
import json
import os

from dotenv import load_dotenv

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
HOME = {"are": "ae", "sau": "sa", "kwt": "kw", "qat": "qa", "bhr": "bh", "omn": "om"}
NAMES = {"are": "ОАЭ", "sau": "Саудовская", "kwt": "Кувейт",
         "qat": "Катар", "bhr": "Бахрейн", "omn": "Оман"}
SUBS = [f"https://{p}.limestore.com/" for p in HOME.values()]
ROOT = "https://limestore.com/"
ARABIC_RE = "[؀-ۿ]"       # арабский блок Unicode
CYRILLIC_RE = "[Ѐ-ӿ]"


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


def q(svc, site, start, end, filters, dims=("date",), limit=25000):
    body = {"startDate": start, "endDate": end, "dimensions": list(dims),
            "dimensionFilterGroups": [{"filters": filters}], "rowLimit": limit, "type": "web"}
    return svc.searchanalytics().query(siteUrl=site, body=body).execute().get("rows", [])


def cc(country):
    return {"dimension": "country", "operator": "equals", "expression": country}


def contains(term):
    return {"dimension": "query", "operator": "contains", "expression": term}


def regex(pattern):
    return {"dimension": "query", "operator": "includingRegex", "expression": pattern}


def totals(rows):
    return (sum(int(r.get("clicks", 0)) for r in rows),
            sum(int(r.get("impressions", 0)) for r in rows))


def script_of(text):
    if any("؀" <= ch <= "ۿ" for ch in text):
        return "арабица"
    if any("Ѐ" <= ch <= "ӿ" for ch in text):
        return "кириллица"
    return "латиница"


def main():
    svc = service()
    end = dt.date.today() - dt.timedelta(days=3)
    start = end - dt.timedelta(weeks=8)
    s, e = start.isoformat(), end.isoformat()

    print(f"== P4: кого обслуживает корневой {ROOT} в Заливе ({s}..{e}) ==")
    for country in ("are", "sau"):
        rows = q(svc, ROOT, s, e, [cc(country)], dims=("query",), limit=200)
        rows.sort(key=lambda r: -int(r.get("impressions", 0)))
        by_script = {}
        for r in rows:
            k = script_of(r["keys"][0])
            c, i = by_script.get(k, (0, 0))
            by_script[k] = (c + int(r.get("clicks", 0)), i + int(r.get("impressions", 0)))
        print(f"\n-- {NAMES[country]}: раскладка топ-200 запросов по алфавиту")
        for k, (c, i) in sorted(by_script.items(), key=lambda x: -x[1][1]):
            print(f"   {k:10} {c:>6} кл / {i:>7} пок")
        print("   топ-10 запросов:")
        for r in rows[:10]:
            print(f"     {r['keys'][0][:44]:46} {script_of(r['keys'][0]):10} "
                  f"{int(r.get('clicks',0)):>5} кл / {int(r.get('impressions',0)):>6} пок")

    print(f"\n== P5: арабский бренд-спрос — фильтр 'لايم' vs весь арабский ({s}..{e}) ==")
    for site in (f"https://ae.limestore.com/", f"https://sa.limestore.com/"):
        for country in ("are", "sau"):
            term = q(svc, site, s, e, [contains("لايم"), cc(country)])
            arab = q(svc, site, s, e, [regex(ARABIC_RE), cc(country)])
            tc, ti = totals(term)
            ac, ai = totals(arab)
            print(f"  {site[8:26]:20} {NAMES[country]:11} 'لايم': {tc:>5} кл /{ti:>6} пок "
                  f"| весь арабский: {ac:>5} кл /{ai:>6} пок")
        rows = q(svc, site, s, e, [regex(ARABIC_RE), cc("are")], dims=("query",), limit=25)
        rows.sort(key=lambda r: -int(r.get("impressions", 0)))
        print(f"  -- топ арабских запросов {site[8:26]} (ОАЭ):")
        for r in rows[:12]:
            print(f"     {r['keys'][0][:40]:42} {int(r.get('clicks',0)):>5} кл / "
                  f"{int(r.get('impressions',0)):>6} пок")

    print(f"\n== P6: задвоение — Σ6 поддоменов vs домашний домен страны ({s}..{e}) ==")
    for country, home in HOME.items():
        tot_c = tot_i = 0
        home_c = home_i = 0
        for site in SUBS:
            c, i = totals(q(svc, site, s, e, [contains("lime"), cc(country)]))
            tot_c += c
            tot_i += i
            if site.startswith(f"https://{home}."):
                home_c, home_i = c, i
        infl = (tot_i / home_i - 1) * 100 if home_i else 0
        print(f"  {NAMES[country]:11} Σ6: {tot_c:>6} кл /{tot_i:>7} пок | "
              f"домашний {home}.: {home_c:>6} кл /{home_i:>7} пок | раздув показов +{infl:.0f}%")

    print("\n== P7: глубина истории GSC на ae.limestore.com ==")
    for months_back in (16, 14, 12, 9, 6):
        d1 = dt.date.today() - dt.timedelta(days=30 * months_back)
        d2 = d1 + dt.timedelta(days=27)
        try:
            c, i = totals(q(svc, "https://ae.limestore.com/", d1.isoformat(), d2.isoformat(),
                            [contains("lime"), cc("are")]))
            print(f"  {d1} .. {d2} ({months_back} мес назад): {c:>6} кл / {i:>7} пок")
        except Exception as ex:  # noqa: BLE001
            print(f"  {d1}: ОШИБКА {type(ex).__name__}: {str(ex)[:100]}")


if __name__ == "__main__":
    main()
