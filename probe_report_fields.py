# -*- coding: utf-8 -*-
"""
probe_report_fields.py — какие поля срезов принимает Reports API Директа.

Поля Device/Gender/Age/HourOfDay/AdNetworkType/TargetingLocationName в репозитории
раньше не использовались, а справочник для CUSTOM_REPORT перечисляет их с оговорками.
Вместо угадывания — пробуем каждое поле отдельным запросом и печатаем вердикт.

Запуск: python probe_report_fields.py
ENV: DIRECT_TOKEN, DIRECT_CLIENTS_JSON
"""

import json
import os
from datetime import date, timedelta

import requests

REPORTS_URL = "https://api.direct.yandex.com/json/v5/reports"

CANDIDATES = [
    "Device",
    "Gender",
    "Age",
    "HourOfDay",
    "AdNetworkType",
    "TargetingLocationName",
    # Числовой идентификатор региона: RegionalAdjustment в API записи требует
    # RegionId, а название («Москва») в него не годится. Пока поле не проверено,
    # региональные корректировки агентом не применяются.
    "TargetingLocationId",
    "LocationOfPresenceName",
    "LocationOfPresenceId",
    "CriterionType",
    "Slot",
]


def _client() -> dict:
    raw = (os.environ.get("DIRECT_CLIENTS_JSON") or "").strip()
    if raw:
        for item in json.loads(raw):
            if isinstance(item, dict) and str(item.get("login", "")).strip():
                return {"login": item["login"], "goals": [str(g) for g in (item.get("goal_ids") or [])]}
    return {"login": os.environ["DIRECT_CLIENT_LOGIN"], "goals": []}


def probe_field(login: str, field: str, date_from: str, date_to: str) -> str:
    params = {
        "SelectionCriteria": {"DateFrom": date_from, "DateTo": date_to},
        "FieldNames": [field, "Clicks", "Cost"],
        "ReportName": f"probe-{field}-{date_from}",
        "ReportType": "CUSTOM_REPORT",
        "DateRangeType": "CUSTOM_DATE",
        "Format": "TSV",
        "IncludeVAT": "YES",
        "IncludeDiscount": "NO",
    }
    resp = requests.post(
        REPORTS_URL,
        data=json.dumps({"params": params}, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {os.environ['DIRECT_TOKEN']}",
            "Client-Login": login,
            "Accept-Language": "ru",
            "processingMode": "offline",
            "returnMoneyInMicros": "false",
            "skipReportHeader": "true",
            "skipReportSummary": "true",
            "Content-Type": "application/json; charset=utf-8",
        },
        timeout=60,
    )
    resp.encoding = "utf-8"
    if resp.status_code in (200, 201, 202):
        return "OK"
    return f"REJECTED http={resp.status_code} {resp.text[:200]}"


def main() -> int:
    client = _client()
    date_to = date.today().isoformat()
    date_from = (date.today() - timedelta(days=7)).isoformat()
    print(f"кабинет: {client['login']}, окно {date_from}..{date_to}\n")
    for field in CANDIDATES:
        print(f"{field:26} {probe_field(client['login'], field, date_from, date_to)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
