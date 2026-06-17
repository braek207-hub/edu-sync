# -*- coding: utf-8 -*-
"""
sync/lime_direct.py — синк кабинета Яндекс Директ для LIME → lime_direct_stats.

Независимый модуль: свой токен/логин Директа, отдельный workflow
(.github/workflows/sync-lime-direct.yml). Не зависит от main.py и sync-lime.yml.

Тянет:
  - Reports API CUSTOM_REPORT (per date+campaign):
      Impressions, Clicks, Cost (с НДС),
      AvgEffectiveBid, AvgTrafficVolume, AvgImpressionPosition, AvgClickPosition,
      BounceRate (%), AvgPageviews.
  - Campaigns.get (снапшот): DailyBudget, BiddingStrategy → weekly_budget, target_cpa.

Запуск:  python -m sync.lime_direct

ENV:
    DATABASE_URL
    LIME_DIRECT_TOKEN
    LIME_DIRECT_CLIENT_LOGIN
    LIME_DIRECT_DAYS_BACK  (default 7)
"""

import os
import io
import csv
import json
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import requests
import psycopg2
import psycopg2.extras

REPORTS_URL = "https://api.direct.yandex.com/json/v5/reports"
CAMPAIGNS_URL = "https://api.direct.yandex.com/json/v5/campaigns"

REPORT_FIELDS = [
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
    "BounceRate",
    "AvgPageviews",
]


def _token() -> str:
    t = os.environ.get("LIME_DIRECT_TOKEN", "").strip()
    if not t:
        raise RuntimeError("LIME_DIRECT_TOKEN не задан")
    return t


def _client_login() -> str:
    login = os.environ.get("LIME_DIRECT_CLIENT_LOGIN", "").strip()
    if not login:
        raise RuntimeError("LIME_DIRECT_CLIENT_LOGIN не задан")
    return login


def _pg_url() -> str:
    return os.environ["DATABASE_URL"].split("?")[0]


def _report_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_token()}",
        "Client-Login": _client_login(),
        "Accept-Language": "en",
        "processingMode": "auto",
        "returnMoneyInMicros": "false",
        "skipReportHeader": "true",
        "skipColumnHeader": "false",
        "skipReportSummary": "true",
    }


def _json_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_token()}",
        "Client-Login": _client_login(),
        "Accept-Language": "en",
        "Content-Type": "application/json; charset=utf-8",
    }


def _num(v: Any) -> float:
    if v is None:
        return 0.0
    s = str(v).strip().replace(",", ".")
    if s in ("", "--"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _int(v: Any) -> int:
    return int(round(_num(v)))


def _micros_to_float(x: Any) -> Optional[float]:
    if x is None or x == "":
        return None
    try:
        return float(x) / 1_000_000.0
    except (ValueError, TypeError):
        return None


def _fetch_report(date_from: str, date_to: str) -> List[Dict[str, Any]]:
    body = {
        "params": {
            "SelectionCriteria": {"DateFrom": date_from, "DateTo": date_to},
            "FieldNames": REPORT_FIELDS,
            "ReportName": f"lime_direct_{date_from}_{date_to}",
            "ReportType": "CUSTOM_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES",
            "IncludeDiscount": "NO",
        }
    }
    payload = json.dumps(body).encode("utf-8")

    for _ in range(8):
        r = requests.post(REPORTS_URL, data=payload, headers=_report_headers())
        r.encoding = "utf-8"
        if r.status_code == 200:
            break
        if r.status_code in (201, 202):
            retry_in = int(r.headers.get("retryIn", "30"))
            print(f"  [lime_direct] отчёт формируется, ждём {retry_in}с...")
            time.sleep(retry_in)
            continue
        raise RuntimeError(f"Reports API {r.status_code}: {r.text[:300]}")
    else:
        raise RuntimeError("Reports API: превышено число попыток")

    rows: List[Dict[str, Any]] = []
    reader = csv.DictReader(io.StringIO(r.text), delimiter="\t")
    for row in reader:
        cid = str(row.get("CampaignId", "")).strip()
        if not cid or cid == "--":
            continue
        rows.append({
            "date": str(row.get("Date", "")).strip(),
            "campaign_id": cid,
            "campaign_name": str(row.get("CampaignName", "")).strip(),
            "impressions": _int(row.get("Impressions")),
            "clicks": _int(row.get("Clicks")),
            "cost": round(_num(row.get("Cost")), 2),
            "avg_effective_bid": round(_num(row.get("AvgEffectiveBid")), 2),
            "avg_traffic_volume": _num(row.get("AvgTrafficVolume")),
            "avg_impression_position": _num(row.get("AvgImpressionPosition")),
            "avg_click_position": _num(row.get("AvgClickPosition")),
            "bounce_rate": _num(row.get("BounceRate")),
            "avg_pageviews": _num(row.get("AvgPageviews")),
        })
    return rows


def _extract_strategy_budget(strategy_block: Any) -> Dict[str, Optional[float]]:
    out: Dict[str, Optional[float]] = {"weekly_budget": None, "target_cpa": None}
    if not isinstance(strategy_block, dict):
        return out
    details = None
    for k, v in strategy_block.items():
        if k in ("BiddingStrategyType", "PlacementTypes"):
            continue
        if isinstance(v, dict):
            details = v
            break
    if not isinstance(details, dict):
        return out
    out["weekly_budget"] = _micros_to_float(details.get("WeeklySpendLimit"))
    cpa = details.get("AverageCpa")
    if cpa is None:
        cpa = details.get("Cpa")
    out["target_cpa"] = _micros_to_float(cpa)
    return out


def _chunked(seq: List[str], n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _fetch_campaigns(campaign_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    info: Dict[str, Dict[str, Any]] = {}
    if not campaign_ids:
        return info

    field_names = ["Id", "Name", "Type", "Status", "State", "DailyBudget"]
    text_fields = ["BiddingStrategy"]
    unified_fields = ["BiddingStrategy"]

    for part in _chunked(campaign_ids, 1000):
        body = {
            "method": "get",
            "params": {
                "SelectionCriteria": {"Ids": [int(x) for x in part]},
                "FieldNames": field_names,
                "TextCampaignFieldNames": text_fields,
                "UnifiedCampaignFieldNames": unified_fields,
            },
        }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        r = requests.post(CAMPAIGNS_URL, data=data, headers=_json_headers())
        r.encoding = "utf-8"
        if r.status_code != 200:
            raise RuntimeError(f"Campaigns API {r.status_code}: {r.text[:300]}")
        js = r.json()
        if "error" in js:
            raise RuntimeError(f"Campaigns API error: {json.dumps(js['error'], ensure_ascii=False)}")

        for c in js.get("result", {}).get("Campaigns", []) or []:
            cid = str(c.get("Id", ""))
            daily = c.get("DailyBudget") or {}
            tc_bs = (c.get("TextCampaign") or {}).get("BiddingStrategy") or {}
            uc_bs = (c.get("UnifiedCampaign") or {}).get("BiddingStrategy") or {}
            search = _extract_strategy_budget(tc_bs.get("Search") or uc_bs.get("Search"))
            network = _extract_strategy_budget(tc_bs.get("Network") or uc_bs.get("Network"))
            weekly = search["weekly_budget"]
            if weekly is None:
                weekly = network["weekly_budget"]
            target_cpa = search["target_cpa"]
            if target_cpa is None:
                target_cpa = network["target_cpa"]
            info[cid] = {
                "weekly_budget": weekly,
                "daily_budget": _micros_to_float(daily.get("Amount")),
                "target_cpa": target_cpa,
                "status": c.get("Status"),
                "state": c.get("State"),
                "campaign_type": c.get("Type"),
            }
    return info


_UPSERT_SQL = """
    INSERT INTO lime_direct_stats
      (date, campaign_id, campaign_name, client_login,
       impressions, clicks, cost,
       avg_effective_bid, avg_traffic_volume,
       avg_impression_position, avg_click_position,
       bounce_rate, avg_pageviews,
       weekly_budget, daily_budget, target_cpa,
       state, status, campaign_type, updated_at)
    VALUES
      (%(date)s, %(campaign_id)s, %(campaign_name)s, %(client_login)s,
       %(impressions)s, %(clicks)s, %(cost)s,
       %(avg_effective_bid)s, %(avg_traffic_volume)s,
       %(avg_impression_position)s, %(avg_click_position)s,
       %(bounce_rate)s, %(avg_pageviews)s,
       %(weekly_budget)s, %(daily_budget)s, %(target_cpa)s,
       %(state)s, %(status)s, %(campaign_type)s, NOW())
    ON CONFLICT (date, campaign_id) DO UPDATE SET
       campaign_name = EXCLUDED.campaign_name,
       client_login = EXCLUDED.client_login,
       impressions = EXCLUDED.impressions,
       clicks = EXCLUDED.clicks,
       cost = EXCLUDED.cost,
       avg_effective_bid = EXCLUDED.avg_effective_bid,
       avg_traffic_volume = EXCLUDED.avg_traffic_volume,
       avg_impression_position = EXCLUDED.avg_impression_position,
       avg_click_position = EXCLUDED.avg_click_position,
       bounce_rate = EXCLUDED.bounce_rate,
       avg_pageviews = EXCLUDED.avg_pageviews,
       weekly_budget = EXCLUDED.weekly_budget,
       daily_budget = EXCLUDED.daily_budget,
       target_cpa = EXCLUDED.target_cpa,
       state = EXCLUDED.state,
       status = EXCLUDED.status,
       campaign_type = EXCLUDED.campaign_type,
       updated_at = NOW()
"""


def _upsert(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    with psycopg2.connect(_pg_url()) as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, _UPSERT_SQL, rows, page_size=500)
        conn.commit()
    return len(rows)


def sync_lime_direct(days_back: int = 7) -> int:
    today = date.today()
    date_from = (today - timedelta(days=days_back)).isoformat()
    date_to = today.isoformat()
    client_login = _client_login()

    print(f"[lime_direct] отчёт {date_from} — {date_to} ({client_login})")
    report_rows = _fetch_report(date_from, date_to)
    print(f"[lime_direct] получено {len(report_rows)} строк отчёта")
    if not report_rows:
        return 0

    campaign_ids = sorted({r["campaign_id"] for r in report_rows})
    campaigns = _fetch_campaigns(campaign_ids)
    print(f"[lime_direct] стратегии/бюджеты по {len(campaigns)} кампаниям")

    merged: List[Dict[str, Any]] = []
    for r in report_rows:
        c = campaigns.get(r["campaign_id"], {})
        merged.append({
            **r,
            "client_login": client_login,
            "weekly_budget": c.get("weekly_budget"),
            "daily_budget": c.get("daily_budget"),
            "target_cpa": c.get("target_cpa"),
            "state": c.get("state"),
            "status": c.get("status"),
            "campaign_type": c.get("campaign_type"),
        })

    n = _upsert(merged)
    print(f"[lime_direct] upsert {n} строк в lime_direct_stats")
    return n


if __name__ == "__main__":
    sync_lime_direct(days_back=int(os.environ.get("LIME_DIRECT_DAYS_BACK", "7")))
