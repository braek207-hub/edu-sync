import csv
import json
import os
import time
from datetime import date, timedelta
from typing import Any, Dict, List

import requests

from sync.classify import detect_direction, detect_project, project_from_client
from sync.utils import to_num_gas

DIRECT_API_URL = "https://api.direct.yandex.com/json/v5/reports"
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 600
MAX_POLL_ATTEMPTS = 30

FIELD_NAMES = [
    "Date",
    "CampaignId",
    "CampaignName",
    "Impressions",
    "Clicks",
    "Cost",
    "AvgEffectiveBid",
    "AvgTrafficVolume",
    "AvgImpressionPosition",
    "AvgClickPosition",
]

DEFAULT_INCREMENTAL_DAYS = 7
DEFAULT_FULL_DATE_FROM = "2026-01-01"


def _direct_clients() -> List[dict]:
    raw_json = os.environ.get("DIRECT_CLIENTS_JSON", "").strip()
    if raw_json:
        clients = json.loads(raw_json)
        out: List[dict] = []
        for item in clients:
            login = str(item.get("login", "")).strip()
            if not login:
                continue
            goals = item.get("goal_ids") or item.get("goals") or []
            out.append(
                {
                    "login": login,
                    "goal_ids": [str(g) for g in goals],
                    "project": project_from_client(login, item),
                    "sheet_name": str(item.get("sheet_name", "")),
                }
            )
        if out:
            return out

    login = os.environ.get("DIRECT_CLIENT_LOGIN", "").strip()
    if login:
        return [{"login": login, "goal_ids": [], "project": None, "sheet_name": ""}]

    raise RuntimeError("Нужен DIRECT_CLIENTS_JSON или DIRECT_CLIENT_LOGIN")


def _report_headers(login: str) -> dict:
    token = os.environ["DIRECT_TOKEN"]
    return {
        "Authorization": f"Bearer {token}",
        "Client-Login": login,
        "Accept-Language": "ru",
        # auto: Директ сам выбирает online (мгновенно) для малых отчётов или offline
        # для крупных. Явный online давал error 8312 на больших аккаунтах EDU.
        # Скорость офлайна обеспечивает быстрый поллинг ниже (retryIn, не 30с-минимум).
        "processingMode": "auto",
        "returnMoneyInMicros": "false",
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
        if len(parts) < 6:
            continue
        padded = parts + [""] * (len(FIELD_NAMES) - len(parts))
        row = dict(zip(FIELD_NAMES, padded[: len(FIELD_NAMES)]))
        campaign_id = str(row.get("CampaignId", "")).strip()
        if not campaign_id or campaign_id == "--":
            continue
        campaign_name = str(row.get("CampaignName", "")).strip()
        impressions = int(float(row.get("Impressions", 0) or 0))
        clicks = int(float(row.get("Clicks", 0) or 0))
        cost = float(row.get("Cost", 0) or 0)

        w_bid = w_traffic = w_impr = w_click = 0.0
        if len(parts) > 6 and clicks > 0:
            w_bid = to_num_gas(parts[6]) * clicks
        if len(parts) > 7 and impressions > 0:
            w_traffic = to_num_gas(parts[7]) * impressions
        if len(parts) > 8 and impressions > 0:
            w_impr = to_num_gas(parts[8]) * impressions
        if len(parts) > 9 and clicks > 0:
            w_click = to_num_gas(parts[9]) * clicks

        rows.append(
            {
                "date": str(row.get("Date", "")).strip(),
                "campaign_id": campaign_id,
                "campaign_name": campaign_name,
                "project": detect_project(campaign_name),
                "direction": detect_direction(campaign_name),
                "cost": cost,
                "clicks": clicks,
                "impressions": impressions,
                "w_avg_eff_bid": w_bid,
                "w_avg_traffic_vol": w_traffic,
                "w_avg_impr_pos": w_impr,
                "w_avg_click_pos": w_click,
                "w_auction_win_share": 0.0,
            }
        )
    return rows


def _report_body(login: str, date_from: str, date_to: str, goals: List[str]) -> dict:
    params: Dict[str, Any] = {
        "SelectionCriteria": {"DateFrom": date_from, "DateTo": date_to},
        "FieldNames": FIELD_NAMES,
        "ReportName": f"edu_sync_{login}_{date_from}_{date_to}",
        "ReportType": "CUSTOM_REPORT",
        "DateRangeType": "CUSTOM_DATE",
        "Format": "TSV",
        "IncludeVAT": "YES",
        "IncludeDiscount": "NO",
    }
    if goals:
        params["Goals"] = goals
        params["AttributionModels"] = ["LSC"]
    return {"params": params}


def _fetch_report(login: str, date_from: str, date_to: str, goals: List[str]) -> List[Dict[str, Any]]:
    headers = _report_headers(login)
    body = _report_body(login, date_from, date_to, goals)

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
            # Уважаем retryIn Директа (обычно несколько секунд), не форсируем 30с минимум.
            retry_in = int(resp.headers.get("retryIn", 5))
            retry_in = max(2, min(retry_in, 60))
            print(f"  [{login}] ждём {retry_in}с (HTTP {resp.status_code})...")
            time.sleep(retry_in)
            continue
        raise RuntimeError(
            f"Директ API [{login}] error {resp.status_code}: {resp.text[:500]}"
        )
    else:
        raise RuntimeError(f"Директ API [{login}]: превышено число попыток")

    return _parse_report_tsv(resp.text)


def sync_direct_api_range(
    date_from: str, date_to: str, *, replace_from: bool = False
) -> int:
    """Загрузка Direct через API за период [date_from, date_to]."""
    clients = _direct_clients()
    print(f"Директ API: {date_from} — {date_to}, клиентов: {len(clients)}")

    all_rows: List[Dict[str, Any]] = []
    errors: List[str] = []
    for client in clients:
        login = client["login"]
        try:
            chunk = _fetch_report(
                login, date_from, date_to, client.get("goal_ids") or []
            )
            print(f"  [{login}] получено {len(chunk)} строк")
            all_rows.extend(chunk)
        except Exception as e:
            print(f"  [{login}] ОШИБКА: {e}")
            errors.append(f"{login}: {e}")

    if errors and not all_rows:
        print(f"Директ API: все клиенты с ошибками: {'; '.join(errors)}")
        return 0
    if not all_rows:
        return 0

    all_rows = [r for r in all_rows if str(r.get("date", "")) >= date_from]
    if not all_rows:
        return 0

    from sync.db import delete_direct_stats_from, upsert_direct_stats

    if replace_from:
        deleted = delete_direct_stats_from(date_from)
        print(f"Директ API: удалено {deleted} строк с {date_from}")

    return upsert_direct_stats(all_rows)


def sync_direct_incremental(days_back: int = DEFAULT_INCREMENTAL_DAYS) -> int:
    """Триггер: последние N дней, upsert (по умолчанию 7)."""
    today = date.today()
    date_from = (today - timedelta(days=days_back)).isoformat()
    date_to = today.isoformat()
    print(f"Директ incremental: {days_back} дн. ({date_from} — {date_to})")
    return sync_direct_api_range(date_from, date_to, replace_from=False)


def sync_direct_full(date_from: str | None = None) -> int:
    """Полная перезагрузка периода с date_from по сегодня (delete + upsert)."""
    start = (date_from or os.environ.get("DIRECT_DATE_FROM") or DEFAULT_FULL_DATE_FROM).strip()
    today = date.today().isoformat()
    print(f"Директ full: {start} — {today}")
    return sync_direct_api_range(start, today, replace_from=True)


def sync_direct_backfill_monthly(
    date_from: str, date_to: str, *, pause_sec: float = 7.0
) -> int:
    """Upsert Direct за период, разбивая на календарные месяцы (без delete)."""
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    if start > end:
        raise ValueError(f"date_from {date_from} > date_to {date_to}")

    total = 0
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        if cursor.month == 12:
            next_month = date(cursor.year + 1, 1, 1)
        else:
            next_month = date(cursor.year, cursor.month + 1, 1)
        month_end = min(end, next_month - timedelta(days=1))
        chunk_from = max(start, cursor).isoformat()
        chunk_to = month_end.isoformat()
        print(f"Директ backfill monthly: {chunk_from} — {chunk_to}")
        n = sync_direct_api_range(chunk_from, chunk_to, replace_from=False)
        print(f"  upsert {n} строк")
        total += n
        if month_end >= end:
            break
        cursor = next_month
        if cursor <= end:
            time.sleep(pause_sec)
    print(f"Директ backfill monthly итого: {total} строк")
    return total


def sync_direct_all() -> int:
    """
    Только Яндекс Direct API.

    DIRECT_SYNC_MODE:
      - incremental (default) — последние DIRECT_DAYS_BACK дней (default 7)
      - full — с DIRECT_DATE_FROM (default 2026-01-01) по сегодня

    DIRECT_SOURCE=sheets — legacy, только если явно задан.
    """
    source = os.environ.get("DIRECT_SOURCE", "api").strip().lower()
    if source == "sheets":
        from sync.direct_sheets import sync_direct_sheets

        print("WARN: DIRECT_SOURCE=sheets (legacy), API отключён")
        return sync_direct_sheets()

    mode = os.environ.get("DIRECT_SYNC_MODE", "incremental").strip().lower()
    if mode in ("monthly_upsert", "backfill_monthly"):
        date_from = (
            os.environ.get("DIRECT_DATE_FROM") or DEFAULT_FULL_DATE_FROM
        ).strip()
        date_to = (
            os.environ.get("DIRECT_DATE_TO") or date.today().isoformat()
        ).strip()
        return sync_direct_backfill_monthly(date_from, date_to)
    if mode in ("full", "full_replace"):
        return sync_direct_full()
    days_back = int(os.environ.get("DIRECT_DAYS_BACK", str(DEFAULT_INCREMENTAL_DAYS)))
    return sync_direct_incremental(days_back=days_back)
