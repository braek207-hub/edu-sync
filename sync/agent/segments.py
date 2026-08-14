# -*- coding: utf-8 -*-
"""
sync/agent/segments.py — загрузчики Директа для автопилота.

Три источника:
  1. сегментные срезы (Reports API, CUSTOM_REPORT) — для корректировок и для
     витрины срезов по кампаниям;
  2. объекты кабинета (adgroups/keywords/ads, API v5) — снимок структуры;
  3. поисковые запросы (SEARCH_QUERY_PERFORMANCE_REPORT).

Reports API асинхронный: 201/202 значат «отчёт готовится», нужен цикл ожидания.
"""

import io
import json
import os
import time
from typing import Any, Dict, List

import requests

REPORTS_URL = "https://api.direct.yandex.com/json/v5/reports"
ADGROUPS_URL = "https://api.direct.yandex.com/json/v5/adgroups"
KEYWORDS_URL = "https://api.direct.yandex.com/json/v5/keywords"
ADS_URL = "https://api.direct.yandex.com/json/v5/ads"

MAX_WAIT_SECONDS = 600
POLL_SECONDS = 10
PAGE_LIMIT = 10_000

# Срез → поле Reports API.
SEGMENT_FIELDS = {
    "device": "Device",
    "gender": "Gender",
    "age": "Age",
    "hour": "HourOfDay",
    "region": "TargetingLocationName",
    "network": "AdNetworkType",
}

_OBJECT_ENDPOINTS = {
    "adgroup": (ADGROUPS_URL, "AdGroups", ["Id", "CampaignId", "Name", "RegionIds", "NegativeKeywords"]),
    "keyword": (KEYWORDS_URL, "Keywords", ["Id", "CampaignId", "AdGroupId", "Keyword", "State", "Status"]),
    "ad": (ADS_URL, "Ads", ["Id", "CampaignId", "AdGroupId", "State", "Status", "Type", "TextAd"]),
}


def _api_headers(login: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {os.environ['DIRECT_TOKEN']}",
        "Client-Login": login,
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
    }


def _report_headers(login: str) -> Dict[str, str]:
    headers = _api_headers(login)
    headers.update({
        "processingMode": "auto",
        "returnMoneyInMicros": "false",
        "skipReportHeader": "true",
        "skipReportSummary": "true",
    })
    return headers


def _run_report(login: str, payload: Dict[str, Any]) -> str:
    """Reports API асинхронный: 201/202 значит «готовится», нужен цикл ожидания."""
    waited = 0
    while waited <= MAX_WAIT_SECONDS:
        resp = requests.post(
            REPORTS_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=_report_headers(login),
            timeout=120,
        )
        if resp.status_code == 200:
            return resp.text
        if resp.status_code in (201, 202):
            time.sleep(POLL_SECONDS)
            waited += POLL_SECONDS
            continue
        raise RuntimeError(f"Reports API {resp.status_code}: {resp.text[:300]}")
    raise TimeoutError(f"Отчёт не готов за {MAX_WAIT_SECONDS} с")


def _parse_tsv(text: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    reader = io.StringIO(text)
    header_line = reader.readline().rstrip("\n")
    if not header_line:
        return out
    header = header_line.split("\t")
    for line in reader:
        cells = line.rstrip("\n").split("\t")
        if len(cells) != len(header):
            continue
        out.append(dict(zip(header, cells)))
    return out


def fetch_segment_report(
    login: str, segment_kind: str, date_from: str, date_to: str, by_campaign: bool = False
) -> List[Dict[str, Any]]:
    """Срез за окно. by_campaign=True добавляет разрез по кампаниям и датам —
    для edu_agent_facts_sliced; без него агрегат по аккаунту для корректировок."""
    field = SEGMENT_FIELDS[segment_kind]
    fields = [field, "Clicks", "Cost", "Impressions", "Conversions"]
    if by_campaign:
        fields = ["CampaignId", "Date"] + fields

    payload = {
        "params": {
            "SelectionCriteria": {"DateFrom": date_from, "DateTo": date_to},
            "FieldNames": fields,
            "ReportName": f"agent-{segment_kind}-{'bycamp-' if by_campaign else ''}{date_from}-{date_to}",
            "ReportType": "CUSTOM_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES",
            "IncludeDiscount": "NO",
        }
    }

    rows: List[Dict[str, Any]] = []
    for rec in _parse_tsv(_run_report(login, payload)):
        row = {
            "segment_kind": segment_kind,
            "segment_key": rec.get(field, ""),
            "slice_key": rec.get(field, ""),
            "clicks": int(rec.get("Clicks") or 0),
            "impressions": int(rec.get("Impressions") or 0),
            "conversions": int(rec.get("Conversions") or 0),
            "cost": float(rec.get("Cost") or 0.0),
        }
        if by_campaign:
            row["campaign_id"] = rec.get("CampaignId", "")
            row["date"] = rec.get("Date", "")
        rows.append(row)
    return rows


def fetch_objects(login: str, object_level: str) -> List[Dict[str, Any]]:
    """Постранично тянет объекты уровня. SelectionCriteria обязан быть непустым:
    у ads.get пустой критерий отклоняется с ошибкой."""
    url, collection, fields = _OBJECT_ENDPOINTS[object_level]
    out: List[Dict[str, Any]] = []
    offset = 0
    while True:
        payload = {
            "method": "get",
            "params": {
                "SelectionCriteria": {"States": ["ON", "OFF", "SUSPENDED"]},
                "FieldNames": fields,
                "Page": {"Limit": PAGE_LIMIT, "Offset": offset},
            },
        }
        resp = requests.post(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=_api_headers(login),
            timeout=120,
        )
        body = resp.json()
        if body.get("error"):
            raise RuntimeError(f"{object_level}.get: {body['error']}")
        result = body.get("result") or {}
        items = result.get(collection) or []
        out += items
        limited_by = result.get("LimitedBy") or 0
        if not limited_by or not items:
            break
        offset = limited_by
    return out


def fetch_search_queries(login: str, date_from: str, date_to: str) -> List[Dict[str, Any]]:
    """Поисковые запросы за окно, агрегат без дат. Только строки с кликами:
    показы без кликов дают миллионы строк и ничего не решают."""
    payload = {
        "params": {
            "SelectionCriteria": {
                "DateFrom": date_from,
                "DateTo": date_to,
                "Filter": [{"Field": "Clicks", "Operator": "GREATER_THAN", "Values": ["0"]}],
            },
            "FieldNames": ["CampaignId", "Query", "Criteria", "Cost", "Clicks", "Conversions"],
            "ReportName": f"agent-queries-{date_from}-{date_to}",
            "ReportType": "SEARCH_QUERY_PERFORMANCE_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES",
            "IncludeDiscount": "NO",
        }
    }
    return [
        {
            "window_from": date_from,
            "window_to": date_to,
            "campaign_id": rec.get("CampaignId", ""),
            "query": rec.get("Query", ""),
            "matched_key": rec.get("Criteria"),
            "cost": float(rec.get("Cost") or 0.0),
            "clicks": int(rec.get("Clicks") or 0),
            "conversions": int(rec.get("Conversions") or 0),
        }
        for rec in _parse_tsv(_run_report(login, payload))
    ]
