# -*- coding: utf-8 -*-
"""
sync/decortier_direct.py — синк кабинета Яндекс Директ Decortier → decortier_direct_stats.

Независимый workflow: .github/workflows/sync-decortier-direct.yml

Запуск:  python -m sync.decortier_direct

ENV:
    DATABASE_URL
    DECORTIER_DIRECT_TOKEN
    DECORTIER_DIRECT_CLIENT_LOGIN   (default: walpapperdecor)
    DECORTIER_DIRECT_DAYS_BACK      (default 7)
"""

import io
import csv
import json
import os
import time
from datetime import date, timedelta
from typing import Any, Dict, List

import psycopg2
import psycopg2.extras
import requests

REPORTS_URL = "https://api.direct.yandex.com/json/v5/reports"
DEFAULT_CLIENT_LOGIN = "walpapperdecor"


def _token() -> str:
    token = (
        os.environ.get("DECORTIER_DIRECT_TOKEN", "").strip()
        or os.environ.get("DIRECT_DECORTIER_TOKEN", "").strip()
        or os.environ.get("DIRECT_DECORTIET_TOKEN", "").strip()
    )
    if not token:
        raise RuntimeError("DECORTIER_DIRECT_TOKEN не задан")
    return token


def _client_login() -> str:
    return (
        os.environ.get("DECORTIER_DIRECT_CLIENT_LOGIN", "").strip()
        or os.environ.get("DIRECT_CLIENT_DECORTIER_LOGIN", "").strip()
        or os.environ.get("DIRECT_CLIENT_DECORTIET_LOGIN", "").strip()
        or DEFAULT_CLIENT_LOGIN
    )


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_token()}",
        "Client-Login": _client_login(),
        "Accept-Language": "ru",
        "processingMode": "auto",
        "returnMoneyInMicros": "false",
        "skipReportHeader": "true",
        "skipColumnHeader": "true",
        "skipReportSummary": "true",
        "Content-Type": "application/json",
    }


def _fetch_report(date_from: str, date_to: str) -> List[Dict[str, Any]]:
    body = {
        "params": {
            "SelectionCriteria": {"DateFrom": date_from, "DateTo": date_to},
            "FieldNames": ["Date", "CampaignId", "CampaignName", "Impressions", "Clicks", "Cost"],
            "ReportName": f"decortier_{date_from}_{date_to}",
            "ReportType": "CAMPAIGN_PERFORMANCE_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES",
            "IncludeDiscount": "NO",
        }
    }
    payload = json.dumps(body).encode("utf-8")

    for _ in range(8):
        r = requests.post(REPORTS_URL, data=payload, headers=_headers())
        r.encoding = "utf-8"
        if r.status_code == 200:
            break
        if r.status_code in (201, 202):
            retry_in = int(r.headers.get("retryIn", "30"))
            print(f"  [decortier_direct] отчёт формируется, ждём {retry_in}с...")
            time.sleep(retry_in)
            continue
        raise RuntimeError(f"Reports API {r.status_code}: {r.text[:300]}")
    else:
        raise RuntimeError("Reports API: превышено число попыток")

    rows: List[Dict[str, Any]] = []
    reader = csv.reader(io.StringIO(r.text.lstrip("\ufeff")), delimiter="\t")
    for parts in reader:
        if len(parts) < 6:
            continue
        cid = str(parts[1]).strip()
        if not cid or cid == "--":
            continue
        rows.append(
            {
                "date": str(parts[0]).strip(),
                "campaign_id": cid,
                "campaign_name": str(parts[2]).strip() or None,
                "client_login": _client_login(),
                "cost": float(parts[5] or 0),
                "clicks": int(float(parts[4] or 0)),
                "impressions": int(float(parts[3] or 0)),
            }
        )
    return rows


def _upsert(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO decortier_direct_stats
                  (date, campaign_id, campaign_name, client_login, cost, clicks, impressions, updated_at)
                VALUES %s
                ON CONFLICT (date, campaign_id) DO UPDATE SET
                  campaign_name = EXCLUDED.campaign_name,
                  client_login = EXCLUDED.client_login,
                  cost = EXCLUDED.cost,
                  clicks = EXCLUDED.clicks,
                  impressions = EXCLUDED.impressions,
                  updated_at = NOW()
                """,
                [
                    (
                        r["date"],
                        r["campaign_id"],
                        r["campaign_name"],
                        r["client_login"],
                        r["cost"],
                        r["clicks"],
                        r["impressions"],
                    )
                    for r in rows
                ],
                template="(%s, %s, %s, %s, %s, %s, %s, NOW())",
            )
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def sync_decortier_direct(days_back: int = 7) -> int:
    today = date.today()
    date_from = (today - timedelta(days=days_back)).isoformat()
    date_to = today.isoformat()
    login = _client_login()

    print(f"[decortier_direct] отчёт {date_from} — {date_to} ({login})")
    report_rows = _fetch_report(date_from, date_to)
    print(f"[decortier_direct] получено {len(report_rows)} строк отчёта")
    n = _upsert(report_rows)
    print(f"[decortier_direct] upsert {n} строк в decortier_direct_stats")
    return n


if __name__ == "__main__":
    sync_decortier_direct(days_back=int(os.environ.get("DECORTIER_DIRECT_DAYS_BACK", "7")))
