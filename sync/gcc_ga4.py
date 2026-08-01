# -*- coding: utf-8 -*-
"""GA4 Data API клиент для GCC (property ae.lime-shop.com = 417919368).

Тянет DAU (`activeUsers`) по дате×стране×каналу тем же сервис-аккаунтом lime-reports, что
пишет в таблицу (scope analytics.readonly). Отдельный лист «GA4» для сверки с Метрикой и с
ручным GA4-файлом Павла. Данные GA4, НЕ Метрика — источники независимы.
"""
from __future__ import annotations

import json
import os

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

_SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]
GA4_PROPERTY = os.environ.get("GCC_GA4_PROPERTY") or "417919368"


def _client():
    """analyticsdata v1beta под SA lime-reports (тот же ключ, что и Sheets, но scope analytics)."""
    sa_json = os.environ.get("LIME_REPORTS_SA_JSON")
    if sa_json:
        creds = Credentials.from_service_account_info(json.loads(sa_json), scopes=_SCOPES)
    else:
        path = os.environ.get("LIME_REPORTS_SA_FILE") or os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
        creds = Credentials.from_service_account_file(path, scopes=_SCOPES)
    return build("analyticsdata", "v1beta", credentials=creds, cache_discovery=False)


def run_report(property_id: str, frm: str, to: str, dimensions: list[str],
               metrics: tuple = ("activeUsers",), dimension_filter: dict | None = None) -> list[dict]:
    """runReport → список строк [{dims: [...], metrics: [...]}]. Даты 'YYYY-MM-DD'."""
    svc = _client()
    body = {
        "dateRanges": [{"startDate": frm, "endDate": to}],
        "dimensions": [{"name": d} for d in dimensions],
        "metrics": [{"name": m} for m in metrics],
        "limit": 250000,
    }
    if dimension_filter:
        body["dimensionFilter"] = dimension_filter
    resp = svc.properties().runReport(property=f"properties/{property_id}", body=body).execute()
    out = []
    for row in resp.get("rows", []):
        out.append({
            "dims": [d.get("value") for d in row.get("dimensionValues", [])],
            "metrics": [m.get("value") for m in row.get("metricValues", [])],
        })
    return out


# hostName → код страны (магазин = поддомен, .lime-shop.com и .limestore.com — один магазин).
# BH исключён (в ручном файле колонки BH нет; GCC = 5 стран).
HOST_COUNTRY = {
    "ae.lime-shop.com": "UAE", "ae.limestore.com": "UAE",
    "sa.lime-shop.com": "KSA", "sa.limestore.com": "KSA",
    "qa.lime-shop.com": "QA", "qa.limestore.com": "QA",
    "kw.lime-shop.com": "KW", "kw.limestore.com": "KW",
    "om.lime-shop.com": "OM", "om.limestore.com": "OM",
}
GA4_CODES = ["UAE", "KSA", "QA", "KW", "OM"]

# Платные каналы GA4 = сравнение «Платный трафик» в UI Павла. ORG = Total−PAID (всё остальное).
_PAID_CH = {"Paid Shopping", "Paid Search", "Paid Social", "Paid Other", "Paid Video",
            "Display", "Cross-network", "Audio"}


def _by_host(property_id: str, dates: list[str], metric: str, paid_only: bool) -> dict:
    """{(iso, code): activeUsers} по стране (hostName→код). paid_only → фильтр платных каналов."""
    dim_filter = None
    if paid_only:
        dim_filter = {"filter": {"fieldName": "sessionDefaultChannelGroup",
                                 "inListFilter": {"values": sorted(_PAID_CH)}}}
    rows = run_report(property_id, min(dates), max(dates), ["date", "hostName"], (metric,), dim_filter)
    dset, out = set(dates), {}
    for r in rows:
        d_raw, host = r["dims"][0], r["dims"][1]
        iso = f"{d_raw[:4]}-{d_raw[4:6]}-{d_raw[6:8]}"  # GA4 отдаёт 'YYYYMMDD'
        code = HOST_COUNTRY.get((host or "").lower())
        if iso in dset and code:
            out[(iso, code)] = out.get((iso, code), 0) + int(r["metrics"][0])
    return out


def fetch_ga4_traffic(property_id: str, dates: list[str], metric: str = "activeUsers") -> dict:
    """{date: {scope: {org, paid}}} по 5 странам Залива + GCC(сумма 5).

    Логика ручного файла Павла: PAID = платные каналы (Paid*+Display+Cross-network+Audio);
    Total = ВСЕ пользователи хоста (без фильтра каналов); **ORG = Total − PAID** (всё неплатное:
    organic+direct+referral+email+unassigned). Два запроса activeUsers: total и paid-фильтр.
    """
    total = _by_host(property_id, dates, metric, paid_only=False)
    paid = _by_host(property_id, dates, metric, paid_only=True)
    out = {d: {s: {"org": 0, "paid": 0} for s in ["GCC"] + GA4_CODES} for d in dates}
    for iso in dates:
        for code in GA4_CODES:
            t, p = total.get((iso, code), 0), paid.get((iso, code), 0)
            org = max(t - p, 0)
            out[iso][code]["paid"] = p
            out[iso][code]["org"] = org
            out[iso]["GCC"]["paid"] += p
            out[iso]["GCC"]["org"] += org
    return out


def validate() -> None:
    """Сверка с ручным файлом: печать per-country ORG/PAID/Total за окно, activeUsers И sessions."""
    import os as _os

    frm = _os.environ.get("LIME_GCC_FROM") or "2025-09-01"
    to = _os.environ.get("LIME_GCC_TO") or "2025-09-03"
    dates = []
    from datetime import date as _date, timedelta as _td
    d = _date.fromisoformat(frm)
    while d <= _date.fromisoformat(to):
        dates.append(d.isoformat()); d += _td(days=1)

    for metric in ("activeUsers", "sessions"):
        data = fetch_ga4_traffic(GA4_PROPERTY, dates, metric)
        print(f"\n=== GA4 {metric} property={GA4_PROPERTY} ({frm}..{to}) ===")
        for iso in dates:
            g = data[iso]["GCC"]
            parts = " | ".join(f"{c} {data[iso][c]['org']}/{data[iso][c]['paid']}" for c in GA4_CODES)
            print(f"{iso}: GCC ORG {g['org']} PAID {g['paid']} Total {g['org'] + g['paid']}  ||  {parts}")


def hosts_probe() -> None:
    """Диагностика расхождения: ВСЕ hostName за день + грандтотал vs сумма замапленных 5 стран."""
    import os as _os
    day = _os.environ.get("LIME_GCC_FROM") or "2026-07-26"

    grand = run_report(GA4_PROPERTY, day, day, ["date"], ("activeUsers",))
    grand_v = int(grand[0]["metrics"][0]) if grand else 0

    rows = run_report(GA4_PROPERTY, day, day, ["hostName"], ("activeUsers",))
    rows.sort(key=lambda r: -int(r["metrics"][0]))
    mapped = 0
    print(f"GA4 hosts за {day}: грандтотал activeUsers(no dim) = {grand_v}")
    print("hostName → activeUsers  [код или НЕ ЗАМАПЛЕН]:")
    for r in rows:
        host = (r["dims"][0] or "").lower()
        v = int(r["metrics"][0])
        code = HOST_COUNTRY.get(host)
        if code:
            mapped += v
        tag = code or "НЕ ЗАМАПЛЕН"
        print(f"  {r['dims'][0]}: {v}  [{tag}]")
    print(f"\nСумма замапленных 5 стран: {mapped} | грандтотал: {grand_v} | вне мапы: {grand_v - mapped}")


def probe() -> None:
    """Разведка: одно ли property покрывает все 5 стран Залива + какие каналы (paid/organic)."""
    from collections import Counter
    from datetime import datetime, timedelta, timezone

    today = (datetime.now(timezone.utc) + timedelta(hours=3)).date()
    to = today - timedelta(days=1)
    frm = to - timedelta(days=1)
    print(f"GA4 property={GA4_PROPERTY}, окно {frm}..{to}")

    rows = run_report(GA4_PROPERTY, frm.isoformat(), to.isoformat(),
                      ["date", "country", "sessionDefaultChannelGroup"], ("activeUsers",))
    print(f"строк: {len(rows)}")
    by_country = Counter()
    channels = Counter()
    for r in rows:
        _, country, channel = r["dims"]
        dau = int(r["metrics"][0])
        by_country[country] += dau
        channels[channel] += dau
    print("\nСтраны (DAU за окно):")
    for c, v in by_country.most_common(20):
        print(f"  {c}: {v}")
    print("\nКаналы (sessionDefaultChannelGroup, DAU):")
    for c, v in channels.most_common(20):
        print(f"  {c}: {v}")
