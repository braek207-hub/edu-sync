from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import time
from collections import defaultdict
from typing import Any

import requests

from .common import (
    SupabaseRest,
    add_date_range_args,
    clean_text,
    env_optional,
    env_required,
    infer_source_type,
    money,
    parse_int,
    resolve_date_range,
)


REPORTS_URL = "https://api.direct.yandex.com/json/v5/reports"


def load_client_logins() -> list[dict[str, str]]:
    """Returns list of dicts with 'login' and 'token' keys."""
    default_token = env_optional("DIRECT_TOKEN")
    raw = env_optional("DIRECT_CLIENTS_JSON")
    if raw:
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise RuntimeError("DIRECT_CLIENTS_JSON must be a JSON array")
        clients: list[dict[str, str]] = []
        for item in parsed:
            if isinstance(item, str):
                clients.append({"login": item.strip(), "token": default_token})
            elif isinstance(item, dict):
                login = str(item.get("login") or item.get("client_login") or "").strip()
                token = str(item.get("token") or "").strip() or default_token
                if login:
                    clients.append({"login": login, "token": token})
        if clients:
            return clients
        raise RuntimeError("DIRECT_CLIENTS_JSON must contain at least one login")
    login = env_required("DIRECT_CLIENT_LOGIN")
    token = env_optional("DIRECT_TOKEN")
    return [{"login": login, "token": token}]


def direct_headers(token: str, login: str, include_column_header: bool = False) -> dict[str, str]:
    return {
        "Authorization": "Bearer " + token,
        "Client-Login": login,
        "Accept-Language": "en",
        "processingMode": "auto",
        "returnMoneyInMicros": "false",
        "skipReportHeader": "true",
        "skipColumnHeader": "false" if include_column_header else "true",
        "skipReportSummary": "true",
    }


def fetch_report(login: str, date_from: str, date_to: str, fields: list[str], *, token: str = "") -> str:
    if not token:
        token = env_required("DIRECT_TOKEN")
    signature = hashlib.md5("|".join(fields + [login, date_from, date_to]).encode("utf-8")).hexdigest()[:10]
    body = {
        "params": {
            "SelectionCriteria": {"DateFrom": date_from, "DateTo": date_to},
            "FieldNames": fields,
            "ReportName": f"MarketingSupabase_{login}_{date_from}_{date_to}_{signature}",
            "ReportType": "CUSTOM_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES",
            "IncludeDiscount": "NO",
        }
    }
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    backoff = 10
    server_error_attempts = 0

    while True:
        response = requests.post(
            REPORTS_URL,
            data=payload,
            headers=direct_headers(token, login, include_column_header=True),
            timeout=120,
        )
        response.encoding = "utf-8"
        if response.status_code == 200:
            return response.text
        if response.status_code == 201:
            time.sleep(60)
            continue
        if response.status_code == 202:
            time.sleep(int(response.headers.get("retryIn", 60)))
            continue
        if response.status_code in {500, 502, 503, 504}:
            server_error_attempts += 1
            if server_error_attempts >= 5:
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)
            continue
        raise RuntimeError(f"Direct API error for {login}: {response.status_code}: {response.text[:500]}")
    raise RuntimeError(f"Direct API error for {login}: {response.status_code}: {response.text[:500]}")


def source_type_from_report(value: str, campaign_name: str) -> str:
    raw = clean_text(value).lower()
    if raw in {"search", "SEARCH".lower()}:
        return "search"
    if raw in {"ad_network", "network", "AD_NETWORK".lower()}:
        return "rsya"
    return infer_source_type(campaign_name)


def fetch_rows(date_from: str, date_to: str) -> list[dict[str, Any]]:
    include_source_type = env_optional("DIRECT_INCLUDE_SOURCE_TYPE", "false").lower() == "true"
    fields = ["Date", "CampaignId", "CampaignName", "Impressions", "Clicks", "Cost"]
    if include_source_type:
        fields.append("AdNetworkType")

    aggregate: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "campaign_name": "",
            "source_types": set(),
            "impressions": 0,
            "clicks": 0,
            "cost_vat": 0.0,
        }
    )

    for client in load_client_logins():
        text = fetch_report(client["login"], date_from, date_to, fields, token=client["token"])
        reader = csv.DictReader(io.StringIO(text), delimiter="\t")
        for row in reader:
            row_date = clean_text(row.get("Date"))
            campaign_id = clean_text(row.get("CampaignId"))
            if not row_date or not campaign_id:
                continue
            campaign_name = clean_text(row.get("CampaignName"))
            item = aggregate[(row_date, campaign_id)]
            if campaign_name:
                item["campaign_name"] = campaign_name
            item["impressions"] += parse_int(row.get("Impressions"))
            item["clicks"] += parse_int(row.get("Clicks"))
            item["cost_vat"] += money(row.get("Cost"))
            detected_type = source_type_from_report(clean_text(row.get("AdNetworkType")), campaign_name)
            if detected_type:
                item["source_types"].add(detected_type)

    rows: list[dict[str, Any]] = []
    for (row_date, campaign_id), values in sorted(aggregate.items()):
        source_types = sorted(values["source_types"])
        rows.append(
            {
                "date": row_date,
                "campaign_id": campaign_id,
                "campaign_name": values["campaign_name"] or campaign_id,
                "source_type": source_types[0] if len(source_types) == 1 else ("mixed" if source_types else ""),
                "impressions": values["impressions"],
                "clicks": values["clicks"],
                "cost_vat": round(values["cost_vat"], 2),
            }
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Yandex Direct daily campaign costs to Supabase staging.")
    add_date_range_args(parser)
    args = parser.parse_args()
    date_from, date_to = resolve_date_range(args)

    rows = fetch_rows(date_from, date_to)
    supabase = SupabaseRest()
    supabase.delete_date_range("stg_direct_daily_campaign", date_from, date_to)
    supabase.upsert("stg_direct_daily_campaign", rows, "date,campaign_id")


if __name__ == "__main__":
    main()
