import csv
import io
import os
import time
from datetime import date, timedelta
from typing import Any, Dict, List

import requests

from sync.classify import detect_direction, detect_project

DIRECT_API_URL = "https://api.direct.yandex.com/json/v5/reports"


def _fetch_report(date_from: str, date_to: str) -> List[Dict[str, Any]]:
    token = os.environ["DIRECT_TOKEN"]
    client_login = os.environ["DIRECT_CLIENT_LOGIN"]

    headers = {
        "Authorization": f"Bearer {token}",
        "Client-Login": client_login,
        "Accept-Language": "ru",
        "processingMode": "auto",
        "returnMoneyInMicros": "false",
    }

    body = {
        "params": {
            "SelectionCriteria": {
                "DateFrom": date_from,
                "DateTo": date_to,
            },
            "FieldNames": [
                "Date",
                "CampaignId",
                "CampaignName",
                "Impressions",
                "Clicks",
                "Cost",
            ],
            "ReportName": f"edu_sync_{date_from}_{date_to}",
            "ReportType": "CAMPAIGN_PERFORMANCE_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "NO",
            "IncludeDiscount": "NO",
        }
    }

    resp = None
    for _ in range(12):
        resp = requests.post(DIRECT_API_URL, json=body, headers=headers, timeout=120)
        if resp.status_code == 200:
            break
        if resp.status_code in (201, 202):
            retry_in = int(resp.headers.get("retryIn", 30))
            print(f"  Отчёт Директа: ждём {retry_in}с (HTTP {resp.status_code})...")
            time.sleep(retry_in)
            continue
        raise RuntimeError(f"Директ API error {resp.status_code}: {resp.text[:500]}")
    else:
        raise RuntimeError("Директ API: превышено число попыток")

    rows: List[Dict[str, Any]] = []
    text = resp.text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    for row in reader:
        campaign_id = str(row.get("CampaignId", "")).strip()
        if not campaign_id or campaign_id == "--":
            continue
        campaign_name = str(row.get("CampaignName", "")).strip()
        rows.append(
            {
                "date": str(row.get("Date", "")).strip(),
                "campaign_id": campaign_id,
                "campaign_name": campaign_name,
                "project": detect_project(campaign_name),
                "direction": detect_direction(campaign_name),
                "cost": float(row.get("Cost", 0) or 0),
                "clicks": int(float(row.get("Clicks", 0) or 0)),
                "impressions": int(float(row.get("Impressions", 0) or 0)),
            }
        )
    return rows


def sync_direct(days_back: int = 7) -> int:
    today = date.today()
    date_from = (today - timedelta(days=days_back)).isoformat()
    date_to = today.isoformat()

    print(f"Директ: запрос за {date_from} — {date_to}")
    rows = _fetch_report(date_from, date_to)
    print(f"Директ: получено {len(rows)} строк")
    if not rows:
        return 0

    from sync.db import upsert_direct_stats

    n = upsert_direct_stats(rows)
    print(f"Директ: upsert {n} строк в direct_stats")
    return n
