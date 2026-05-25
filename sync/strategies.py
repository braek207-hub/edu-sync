"""Синк strategies_daily из Google Sheets → strategy_snapshots."""

from __future__ import annotations

import os
from typing import Any, Dict, List

from sync.sheets import get_sheets_service, read_sheet
from sync.utils import normalize_campaign_id, pick_index_loose, to_iso_date, to_num

STRATEGIES_SHEET = "strategies_daily"


def _money(v: Any) -> float:
    n = to_num(v)
    if n > 10_000_000:
        return n / 1_000_000
    return n


def sync_strategies_daily() -> int:
    service = get_sheets_service()
    spreadsheet_id = os.environ["GOOGLE_SHEETS_ID"]
    values = read_sheet(service, spreadsheet_id, STRATEGIES_SHEET)
    if len(values) < 2:
        print("Стратегии: пустой лист strategies_daily")
        return 0

    headers = [str(x).strip().lower() for x in values[0]]
    si = {
        "date": pick_index_loose(headers, ["date", "дата"]),
        "campaign_id": pick_index_loose(
            headers,
            ["campaignid", "campaign_id", "id камп", "id_камп", "campaign id"],
        ),
        "weekly_budget": pick_index_loose(
            headers,
            [
                "weekly_budget",
                "weekly budget",
                "week_budget",
                "budget_week",
                "недел",
                "weeklylimit",
                "weekly_limit",
            ],
        ),
        "target_cpa": pick_index_loose(
            headers,
            ["target_cpa", "target cpa", "cpa_target", "targetcpa", "целев", "цель", "tcpa"],
        ),
        "state": pick_index_loose(headers, ["state"]),
        "status": pick_index_loose(headers, ["status"]),
    }
    if si["date"] == -1 or si["campaign_id"] == -1:
        raise ValueError("Стратегии: нужны колонки date и campaignId")

    rows: List[Dict[str, Any]] = []
    for r in values[1:]:
        date_iso = to_iso_date(r[si["date"]] if si["date"] < len(r) else "")
        cid = normalize_campaign_id(r[si["campaign_id"]] if si["campaign_id"] < len(r) else "")
        if not date_iso or not cid:
            continue
        rows.append(
            {
                "date": date_iso,
                "campaign_id": cid,
                "campaign_name": "",
                "weekly_budget": round(
                    _money(r[si["weekly_budget"]] if si["weekly_budget"] != -1 and si["weekly_budget"] < len(r) else 0),
                    2,
                ),
                "target_cpa": round(
                    _money(r[si["target_cpa"]] if si["target_cpa"] != -1 and si["target_cpa"] < len(r) else 0),
                    2,
                ),
                "state": str(r[si["state"]] if si["state"] != -1 and si["state"] < len(r) else ""),
                "status": str(r[si["status"]] if si["status"] != -1 and si["status"] < len(r) else ""),
            }
        )

    print(f"Стратегии: {len(rows)} строк из strategies_daily")
    from sync.db import upsert_strategy_snapshots

    n = upsert_strategy_snapshots(rows)
    print(f"Стратегии: upsert {n} строк в strategy_snapshots")
    return n
