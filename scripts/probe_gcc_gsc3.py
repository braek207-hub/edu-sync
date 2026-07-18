# -*- coding: utf-8 -*-
"""Зонд GSC для GCC, раунд 3: подбор бренд-фильтра и старт истории. В БД не пишет.

Запуск: python scripts/probe_gcc_gsc3.py

P8  — покрытие бренд-фильтра. Кандидаты: 'lime' (contains, как в KZ) vs regex
      с арабскими вариантами vs regex с опечаткой 'lim'. Сколько показов ловит
      каждый и какой мусор тащит (топ пойманного, которого нет у более узкого).
P9  — что бренд-фильтр ПРОПУСКАЕТ: топ запросов сайта, не пойманных regex'ом.
P10 — точный старт истории property (помесячно сен-2025 .. янв-2026) → с какой
      даты бэкфиллить.
P11 — задвоение одного SERP: для топ-запросов сравнить Σ показов по property
      против max по property (модель «спрос = max, клики = Σ»).
"""
import datetime as dt
import json
import os

from dotenv import load_dotenv

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
SUBS = [f"https://{p}.limestore.com/" for p in ("ae", "sa", "kw", "qa", "bh", "om")]
ROOT = "https://limestore.com/"
GULF = ["are", "sau", "kwt", "qat", "bhr", "omn"]

NARROW = "lime"                              # как в KZ (contains)
RE_ARAB = "(?i)(lime|limé|leem|لايم|ليم)"     # + арабские варианты
RE_WIDE = "(?i)(lim|لايم|ليم|leem)"           # + опечатка 'lim' (ловит lime/limé/limestore)


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


def totals(rows):
    return (sum(int(r.get("clicks", 0)) for r in rows),
            sum(int(r.get("impressions", 0)) for r in rows))


def main():
    svc = service()
    end = dt.date.today() - dt.timedelta(days=3)
    start = end - dt.timedelta(weeks=8)
    s, e = start.isoformat(), end.isoformat()
    ae = "https://ae.limestore.com/"

    print(f"== P8: покрытие бренд-фильтра, ae. + sa., {s}..{e} ==")
    variants = [
        ("contains 'lime' (как KZ)", {"dimension": "query", "operator": "contains", "expression": NARROW}),
        ("regex + арабский", {"dimension": "query", "operator": "includingRegex", "expression": RE_ARAB}),
        ("regex + 'lim' (опечатки)", {"dimension": "query", "operator": "includingRegex", "expression": RE_WIDE}),
    ]
    for site in (ae, "https://sa.limestore.com/"):
        country = "are" if site == ae else "sau"
        allc, alli = totals(q(svc, site, s, e, [cc(country)]))
        print(f"\n-- {site[8:26]} / {country}: весь сайт {allc} кл / {alli} пок")
        for label, f in variants:
            c, i = totals(q(svc, site, s, e, [f, cc(country)]))
            print(f"   {label:28} {c:>6} кл /{i:>7} пок  ({i / alli * 100:.0f}% показов сайта)")

    print(f"\n== P9: что 'regex + арабский' ПРОПУСКАЕТ (топ-25 непойманных, ae./ОАЭ) ==")
    caught = {r["keys"][0] for r in q(svc, ae, s, e, [
        {"dimension": "query", "operator": "includingRegex", "expression": RE_ARAB}, cc("are")],
        dims=("query",), limit=5000)}
    rows = q(svc, ae, s, e, [cc("are")], dims=("query",), limit=5000)
    missed = [r for r in rows if r["keys"][0] not in caught]
    missed.sort(key=lambda r: -int(r.get("impressions", 0)))
    mc, mi = totals(missed)
    print(f"   всего непойманных: {mc} кл / {mi} пок")
    for r in missed[:25]:
        print(f"     {r['keys'][0][:44]:46} {int(r.get('clicks',0)):>5} кл / "
              f"{int(r.get('impressions',0)):>6} пок")

    print("\n== P10: старт истории property (помесячно, ae./ОАЭ) ==")
    for ym in ("2025-08", "2025-09", "2025-10", "2025-11"):
        y, m = map(int, ym.split("-"))
        d1 = dt.date(y, m, 1)
        d2 = (dt.date(y + m // 12, m % 12 + 1, 1) - dt.timedelta(days=1))
        c, i = totals(q(svc, ae, d1.isoformat(), d2.isoformat(), [cc("are")]))
        print(f"   {ym}: {c:>7} кл / {i:>8} пок")

    print(f"\n== P11: задвоение SERP — Σ по property vs max по property ==")
    for country in GULF:
        per_query: dict[str, list[int]] = {}
        clicks_sum = 0
        for site in SUBS + [ROOT]:
            for r in q(svc, site, s, e, [
                {"dimension": "query", "operator": "includingRegex", "expression": RE_ARAB},
                cc(country)], dims=("query",), limit=5000):
                per_query.setdefault(r["keys"][0], []).append(int(r.get("impressions", 0)))
                clicks_sum += int(r.get("clicks", 0))
        s_imp = sum(sum(v) for v in per_query.values())
        m_imp = sum(max(v) for v in per_query.values())
        dup = len([v for v in per_query.values() if len(v) > 1])
        print(f"   {country}: Σ={s_imp:>7} пок | max={m_imp:>7} пок | "
              f"задвоение {(s_imp / m_imp - 1) * 100 if m_imp else 0:>5.0f}% | "
              f"запросов в 2+ property: {dup}/{len(per_query)} | кликов Σ={clicks_sum}")


if __name__ == "__main__":
    main()
