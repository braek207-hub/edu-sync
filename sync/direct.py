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
# Директ часто отвечает 201/202 и отдаёт TSV через несколько минут
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 600
MAX_POLL_ATTEMPTS = 30


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


FIELD_NAMES = [
    "Date",
    "CampaignId",
    "CampaignName",
    "Impressions",
    "Clicks",
    "Cost",
]


def _report_headers(login: str) -> dict:
    token = os.environ["DIRECT_TOKEN"]
    return {
        "Authorization": f"Bearer {token}",
        "Client-Login": login,
        "Accept-Language": "ru",
        "processingMode": "auto",
        "returnMoneyInMicros": "false",
        # как в BJ_auto_metrica — TSV без строки заголовков
        "skipReportHeader": "true",
        "skipColumnHeader": "true",
        "skipReportSummary": "true",
    }


def _parse_report_tsv(text: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    lines = [ln for ln in text.lstrip("\ufeff").splitlines() if ln.strip()]
    if not lines:
        return rows

    reader = csv.reader(lines, delimiter="\t")
    for parts in reader:
        if len(parts) < len(FIELD_NAMES):
            continue
        row = dict(zip(FIELD_NAMES, parts))
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


def _fetch_report(
    login: str, date_from: str, date_to: str
) -> List[Dict[str, Any]]:
    headers = _report_headers(login)

    body = {
        "params": {
            "SelectionCriteria": {
                "DateFrom": date_from,
                "DateTo": date_to,
            },
            "FieldNames": FIELD_NAMES,
            "ReportName": f"edu_sync_{login}_{date_from}_{date_to}",
            "ReportType": "CAMPAIGN_PERFORMANCE_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "NO",
            "IncludeDiscount": "NO",
        }
    }

    resp = None
    for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
        try:
            resp = requests.post(
                DIRECT_API_URL,
                json=body,
                headers=headers,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
        except requests.exceptions.Timeout as e:
            print(f"  [{login}] timeout (попытка {attempt}/{MAX_POLL_ATTEMPTS}): {e}")
            time.sleep(min(60, 15 + attempt * 5))
            continue

        if resp.status_code == 200:
            break
        if resp.status_code in (201, 202):
            retry_in = int(resp.headers.get("retryIn", 60))
            retry_in = max(30, min(retry_in, 120))
            print(
                f"  [{login}] отчёт в очереди, ждём {retry_in}с "
                f"(HTTP {resp.status_code}, попытка {attempt})..."
            )
            time.sleep(retry_in)
            continue
        raise RuntimeError(
            f"Директ API [{login}] error {resp.status_code}: {resp.text[:500]}"
        )
    else:
        raise RuntimeError(f"Директ API [{login}]: превышено число попыток ({MAX_POLL_ATTEMPTS})")

    rows = _parse_report_tsv(resp.text)
    if not rows and resp.text.strip():
        preview = resp.text[:300].replace("\n", "\\n")
        print(f"  [{login}] предупреждение: TSV без строк данных, превью: {preview}")
    return rows


def sync_direct(days_back: int = 7) -> int:
    today = date.today()
    date_from = (today - timedelta(days=days_back)).isoformat()
    date_to = today.isoformat()

    clients = _direct_clients()
    print(f"Директ: запрос за {date_from} — {date_to}, клиентов: {len(clients)}")

    all_rows: List[Dict[str, Any]] = []
    errors: List[str] = []
    for login, _goals in clients:
        try:
            chunk = _fetch_report(login, date_from, date_to)
            print(f"  [{login}] получено {len(chunk)} строк")
            all_rows.extend(chunk)
        except Exception as e:
            msg = f"{login}: {e}"
            print(f"  [{login}] ОШИБКА: {e}")
            errors.append(msg)

    if errors and not all_rows:
        raise RuntimeError("; ".join(errors))
    if errors:
        print(f"Директ: предупреждения ({len(errors)}): {'; '.join(errors)}")

    print(f"Директ: всего {len(all_rows)} строк")
    if not all_rows:
        return 0

    from sync.db import upsert_direct_stats

    n = upsert_direct_stats(all_rows)
    print(f"Директ: upsert {n} строк в direct_stats")
    return n
