import csv
import io
import json
import os
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Tuple

import requests

from sync.classify import detect_direction, detect_project

DIRECT_API_URL = "https://api.direct.yandex.com/json/v5/reports"


def _direct_clients() -> List[Tuple[str, List[str]]]:
    """
    Список (login, goal_ids) из DIRECT_CLIENTS_JSON (как BJ_auto_metrica)
    или один клиент из DIRECT_CLIENT_LOGIN.
    """
    raw_json = os.environ.get("DIRECT_CLIENTS_JSON", "").strip()
    if raw_json:
        clients = json.loads(raw_json)
        out: List[Tuple[str, List[str]]] = []
        for item in clients:
            login = str(item.get("login", "")).strip()
            if not login:
                continue
            goals = item.get("goal_ids") or item.get("goals") or []
            out.append((login, [str(g) for g in goals]))
        if out:
            return out

    login = os.environ.get("DIRECT_CLIENT_LOGIN", "").strip()
    if login:
        return [(login, [])]

    raise RuntimeError("Нужен DIRECT_CLIENTS_JSON или DIRECT_CLIENT_LOGIN")


def _fetch_report(
    login: str, date_from: str, date_to: str
) -> List[Dict[str, Any]]:
    token = os.environ["DIRECT_TOKEN"]

    headers = {
        "Authorization": f"Bearer {token}",
        "Client-Login": login,
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
            "ReportName": f"edu_sync_{login}_{date_from}_{date_to}",
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
            print(f"  [{login}] отчёт: ждём {retry_in}с (HTTP {resp.status_code})...")
            time.sleep(retry_in)
            continue
        raise RuntimeError(
            f"Директ API [{login}] error {resp.status_code}: {resp.text[:500]}"
        )
    else:
        raise RuntimeError(f"Директ API [{login}]: превышено число попыток")

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

    clients = _direct_clients()
    print(f"Директ: запрос за {date_from} — {date_to}, клиентов: {len(clients)}")

    all_rows: List[Dict[str, Any]] = []
    for login, _goals in clients:
        chunk = _fetch_report(login, date_from, date_to)
        print(f"  [{login}] получено {len(chunk)} строк")
        all_rows.extend(chunk)

    print(f"Директ: всего {len(all_rows)} строк")
    if not all_rows:
        return 0

    from sync.db import upsert_direct_stats

    n = upsert_direct_stats(all_rows)
    print(f"Директ: upsert {n} строк в direct_stats")
    return n
