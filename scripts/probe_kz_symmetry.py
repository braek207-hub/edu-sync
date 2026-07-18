# -*- coding: utf-8 -*-
"""Зонд симметрии KZ ⇄ GCC: занижен ли казахстанский спрос. В БД не пишет.

KZ считается по одному ресурсу (корневой limestore.com), GCC — по семи, включая
корневой. Раз корневой отдаёт показы Заливу, витрины Залива могут отдавать показы
Казахстану. Если объём заметный — KZ занижен и его набор ресурсов надо расширять.

Запуск: python scripts/probe_kz_symmetry.py
"""
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

from sync.brand_terms import brand_regex
from sync.gsc import REGIONS

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
ROOT = "https://limestore.com/"


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


def totals(svc, site, start, end, country, region):
    body = {
        "startDate": start, "endDate": end, "dimensions": ["date"],
        "dimensionFilterGroups": [{"filters": [
            {"dimension": "query", "operator": "includingRegex",
             "expression": brand_regex(region)},
            {"dimension": "country", "operator": "equals", "expression": country},
        ]}],
        "rowLimit": 25000, "type": "web",
    }
    rows = svc.searchanalytics().query(siteUrl=site, body=body).execute().get("rows", [])
    return (sum(int(r.get("clicks", 0)) for r in rows),
            sum(int(r.get("impressions", 0)) for r in rows))


def main():
    svc = service()
    end = dt.date.today() - dt.timedelta(days=3)
    start = end - dt.timedelta(weeks=8)
    s, e = start.isoformat(), end.isoformat()

    print(f"== Казахстан на витринах Залива, {s}..{e} ==")
    root_c, root_i = totals(svc, ROOT, s, e, "kaz", "kz")
    print(f"  корневой limestore.com (как сейчас в KZ): {root_c:>7} кл / {root_i:>8} пок")

    extra_c = extra_i = 0
    for site in REGIONS["gcc"]["sites"]:
        if site == ROOT:
            continue
        c, i = totals(svc, site, s, e, "kaz", "kz")
        extra_c += c
        extra_i += i
        mark = "" if i else "  ·"
        print(f"  {site[8:26]:22} {c:>7} кл / {i:>8} пок{mark}")

    share = extra_i / root_i * 100 if root_i else 0
    print(f"\n  витрины Залива дают Казахстану: {extra_c} кл / {extra_i} пок "
          f"= {share:.1f}% от текущего KZ")
    print("  вывод:", "KZ занижен, набор ресурсов расширить"
          if share >= 2 else "асимметрия несущественна, KZ оставить на корневом")

    print(f"\n== Обратная сторона: Залив на витринах Залива vs корневом ==")
    for country, name in (("are", "ОАЭ"), ("sau", "Саудовская")):
        rc, ri = totals(svc, ROOT, s, e, country, "gcc")
        print(f"  {name}: корневой {rc} кл / {ri} пок (уже учтён в GCC)")


if __name__ == "__main__":
    main()
