# -*- coding: utf-8 -*-
"""Зонд GSC: какие написания бренда реально ищут — по регионам. В БД не пишет.

Запуск: python scripts/probe_brand_spellings.py

Термины бренда должны быть настройкой на регион (Павел, 2026-07-18): у каждого
рынка своя специфика — транслитерации, опечатки, слепая раскладка, местный алфавит.
Зонд меряет объём каждого кандидата, чтобы список собирался по данным, а не на глаз.

P12 — кандидаты по GCC (ОАЭ/Саудовская): латиница, опечатки, арабские варианты.
P13 — кандидаты по KZ: латиница, кириллица, слепая раскладка.
P14 — что остаётся непойманным итоговым набором (топ-25) — не потеряли ли вариант.
"""
import datetime as dt
import json
import os

from dotenv import load_dotenv

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
AE = "https://ae.limestore.com/"
SA = "https://sa.limestore.com/"
ROOT = "https://limestore.com/"

# Кандидаты: (метка, что искать). Слепая раскладка — то, что печатают, не переключив язык.
GCC_CANDIDATES = [
    ("lime (база)", "lime"),
    ("lim (опечатка/сокр.)", "lim"),
    ("limé (с акцентом)", "limé"),
    ("leem (транслит)", "leem"),
    ("liem (опечатка)", "liem"),
    ("limme (опечатка)", "limme"),
    ("laim (транслит)", "laim"),
    ("لايم (арабский)", "لايم"),
    ("ليم (арабский кратк.)", "ليم"),
    ("ليمي (арабский)", "ليمي"),
    ("لييم (арабский опеч.)", "لييم"),
]

KZ_CANDIDATES = [
    ("lime (база)", "lime"),
    ("лайм (кириллица)", "лайм"),
    ("lim (опечатка/сокр.)", "lim"),
    ("лайм через е (лаим)", "лаим"),
    ("дшьу (lime на рус. раскл.)", "дшьу"),
    ("kfqv (лайм на англ. раскл.)", "kfqv"),
    ("laim (транслит)", "laim"),
    ("лиме (опечатка)", "лиме"),
]

# Итоговые наборы-кандидаты для сравнения покрытия (P14).
RE_GCC = "(?i)(lim|leem|liem|laim|لايم|ليم)"
RE_KZ = "(?i)(lim|лайм|лаим|дшьу|kfqv|laim)"


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


def measure(svc, site, s, e, country, candidates, label):
    allc, alli = totals(q(svc, site, s, e, [cc(country)]))
    print(f"\n-- {label}: весь сайт {allc} кл / {alli} пок")
    for name, term in candidates:
        try:
            c, i = totals(q(svc, site, s, e, [contains(term), cc(country)]))
            mark = "  ·" if i == 0 else ""
            print(f"   {name:28} {c:>6} кл /{i:>7} пок{mark}")
        except Exception as ex:  # noqa: BLE001
            print(f"   {name:28} ОШИБКА {type(ex).__name__}: {str(ex)[:80]}")


def missed(svc, site, s, e, country, pattern, label):
    caught = {r["keys"][0] for r in q(svc, site, s, e, [regex(pattern), cc(country)],
                                      dims=("query",), limit=5000)}
    rows = q(svc, site, s, e, [cc(country)], dims=("query",), limit=5000)
    rest = [r for r in rows if r["keys"][0] not in caught]
    rest.sort(key=lambda r: -int(r.get("impressions", 0)))
    cc_, ci = totals([r for r in rows if r["keys"][0] in caught])
    print(f"\n-- {label}: набор ловит {cc_} кл / {ci} пок. Топ непойманного:")
    for r in rest[:25]:
        print(f"     {r['keys'][0][:44]:46} {int(r.get('clicks',0)):>5} кл / "
              f"{int(r.get('impressions',0)):>6} пок")


def main():
    svc = service()
    end = dt.date.today() - dt.timedelta(days=3)
    start = end - dt.timedelta(weeks=8)
    s, e = start.isoformat(), end.isoformat()

    print(f"== P12: кандидаты написаний, GCC ({s}..{e}) ==")
    measure(svc, AE, s, e, "are", GCC_CANDIDATES, "ae.limestore.com / ОАЭ")
    measure(svc, SA, s, e, "sau", GCC_CANDIDATES, "sa.limestore.com / Саудовская")

    print(f"\n== P13: кандидаты написаний, KZ ({s}..{e}) ==")
    measure(svc, ROOT, s, e, "kaz", KZ_CANDIDATES, "limestore.com / Казахстан")

    print(f"\n== P14: что набор пропускает ==")
    missed(svc, AE, s, e, "are", RE_GCC, f"GCC {RE_GCC}, ОАЭ")
    missed(svc, ROOT, s, e, "kaz", RE_KZ, f"KZ {RE_KZ}, Казахстан")


if __name__ == "__main__":
    main()
