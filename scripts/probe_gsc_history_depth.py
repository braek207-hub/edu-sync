# -*- coding: utf-8 -*-
"""Зонд: есть ли в GSC данные РАНЬШЕ того, что мы уже залили. В БД не пишет.

На графике Paid Brand начинается с июня 2025, а спрос/SEO — только с конца сентября.
Две возможные причины, и они требуют разных действий:
  а) ресурс верифицирован в сентябре → GSC физически не отдаёт раньше, добрать нельзя;
  б) мы просто не запрашивали более ранний период → добираем бэкфиллом.
GSC хранит ~16 месяцев, поэтому смотрим помесячно с марта 2025.

Запуск: python scripts/probe_gsc_history_depth.py
"""
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

from sync.brand_terms import brand_regex

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
ROOT = "https://limestore.com/"
AE = "https://ae.limestore.com/"
MONTHS = ["2025-03", "2025-04", "2025-05", "2025-06", "2025-07", "2025-08", "2025-09", "2025-10"]


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


def month_bounds(ym: str) -> tuple[str, str]:
    y, m = map(int, ym.split("-"))
    d1 = dt.date(y, m, 1)
    d2 = dt.date(y + m // 12, m % 12 + 1, 1) - dt.timedelta(days=1)
    return d1.isoformat(), d2.isoformat()


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
    try:
        rows = svc.searchanalytics().query(siteUrl=site, body=body).execute().get("rows", [])
    except Exception as ex:  # noqa: BLE001 — зонд: печатаем и идём дальше
        print(f"      ОШИБКА {type(ex).__name__}: {str(ex)[:120]}")
        return (0, 0)
    return (sum(int(r.get("clicks", 0)) for r in rows),
            sum(int(r.get("impressions", 0)) for r in rows))


def scan(svc, site, country, region, label):
    print(f"\n-- {label}")
    first = None
    for ym in MONTHS:
        s, e = month_bounds(ym)
        c, i = totals(svc, site, s, e, country, region)
        mark = "" if i else "   ·пусто"
        print(f"   {ym}: {c:>7} кл / {i:>8} пок{mark}")
        if i and first is None:
            first = ym
    print(f"   первый месяц с данными: {first or 'нет данных за весь период'}")
    return first


def main():
    svc = service()
    print("== Глубина истории GSC до нашей текущей нижней границы (2025-09-29) ==")

    scan(svc, ROOT, "kaz", "kz", "KZ: limestore.com × Казахстан")
    scan(svc, ROOT, "are", "gcc", "GCC: limestore.com (корневой) × ОАЭ")
    scan(svc, AE, "are", "gcc", "GCC: ae.limestore.com × ОАЭ")

    print("\n== Что отдаёт API на границе окна (самая ранняя доступная дата) ==")
    for site in (ROOT, AE):
        body = {
            "startDate": "2024-01-01", "endDate": dt.date.today().isoformat(),
            "dimensions": ["date"], "rowLimit": 1, "type": "web",
        }
        try:
            rows = svc.searchanalytics().query(siteUrl=site, body=body).execute().get("rows", [])
            print(f"   {site}: самая ранняя дата = {rows[0]['keys'][0] if rows else 'нет строк'}")
        except Exception as ex:  # noqa: BLE001
            print(f"   {site}: ОШИБКА {type(ex).__name__}: {str(ex)[:120]}")


if __name__ == "__main__":
    main()
