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
               metrics: tuple = ("activeUsers",)) -> list[dict]:
    """runReport → список строк [{dims: [...], metrics: [...]}]. Даты 'YYYY-MM-DD'."""
    svc = _client()
    body = {
        "dateRanges": [{"startDate": frm, "endDate": to}],
        "dimensions": [{"name": d} for d in dimensions],
        "metrics": [{"name": m} for m in metrics],
        "limit": 250000,
    }
    resp = svc.properties().runReport(property=f"properties/{property_id}", body=body).execute()
    out = []
    for row in resp.get("rows", []):
        out.append({
            "dims": [d.get("value") for d in row.get("dimensionValues", [])],
            "metrics": [m.get("value") for m in row.get("metricValues", [])],
        })
    return out


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
