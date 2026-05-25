"""Синк Директа из листов Google Sheets — как GAS readDirect_ (НДС и метрики трафика из BJ)."""

from __future__ import annotations

import os
from typing import Any, Dict, List

from sync.classify import DIRECT_SHEETS, detect_direction, resolve_row_project
from sync.sheets import get_sheets_service, read_sheet
from sync.utils import normalize_campaign_id, pick_index_loose, to_iso_date, to_num

# Заголовки листов (BJ FIELDS / gas direct.js)
_CORE = ["date", "campaignid", "campaignname", "impressions", "clicks", "cost"]


def _pick_metric(headers: List[str], variants: List[str]) -> int:
    h = [str(x or "").strip().lower() for x in headers]
    vars_sorted = sorted({v.lower() for v in variants if v}, key=len, reverse=True)
    for i, hh in enumerate(h):
        if not hh or "weighted" in hh or "взвешен" in hh:
            continue
        for v in vars_sorted:
            if v in hh:
                return i
    return -1


def _parse_sheet(
    headers: List[str], values: List[List[Any]], sheet_project: str
) -> List[Dict[str, Any]]:
    if len(values) < 2:
        return []

    hlow = [str(x or "").strip().lower() for x in headers]
    idx = {
        "date": pick_index_loose(hlow, ["date"], 0),
        "cid": pick_index_loose(hlow, ["campaignid", "campaign id"], 1),
        "cname": pick_index_loose(hlow, ["campaignname", "campaign name"], 2),
        "impr": pick_index_loose(hlow, ["impressions"], 3),
        "clicks": pick_index_loose(hlow, ["clicks"], 4),
        "cost": pick_index_loose(hlow, ["cost"], 5),
        "bid": _pick_metric(
            headers,
            ["avgeffectivebid", "avg effective bid", "эффективн"],
        ),
        "traffic": _pick_metric(
            headers,
            ["avgtrafficvolume", "avg traffic volume", "объём трафик", "объем трафик"],
        ),
        "impr_pos": _pick_metric(
            headers,
            ["avgimpressionposition", "average impression position", "позиция показа"],
        ),
        "click_pos": _pick_metric(
            headers,
            ["avgclickposition", "average click position", "позиция клика"],
        ),
        "win": _pick_metric(
            headers,
            [
                "impressionshare",
                "searchimpressionshare",
                "auctionwin",
                "доля выигрыш",
            ],
        ),
    }

    out: List[Dict[str, Any]] = []
    for row in values[1:]:
        if idx["date"] >= len(row):
            continue
        date_iso = to_iso_date(row[idx["date"]])
        if not date_iso:
            continue
        cid = normalize_campaign_id(row[idx["cid"]] if idx["cid"] < len(row) else "")
        if not cid:
            continue
        cname = str(row[idx["cname"]] if idx["cname"] < len(row) else "").strip()
        impressions = int(to_num(row[idx["impr"]] if idx["impr"] < len(row) else 0))
        clicks = int(to_num(row[idx["clicks"]] if idx["clicks"] < len(row) else 0))
        cost = to_num(row[idx["cost"]] if idx["cost"] < len(row) else 0)

        w_bid = w_traffic = w_impr = w_click = w_win = 0.0
        if idx["bid"] != -1 and clicks > 0:
            w_bid = to_num(row[idx["bid"]]) * clicks
        if idx["traffic"] != -1 and impressions > 0:
            w_traffic = to_num(row[idx["traffic"]]) * impressions
        if idx["impr_pos"] != -1 and impressions > 0:
            w_impr = to_num(row[idx["impr_pos"]]) * impressions
        if idx["click_pos"] != -1 and clicks > 0:
            w_click = to_num(row[idx["click_pos"]]) * clicks
        if idx["win"] != -1 and impressions > 0:
            raw = to_num(row[idx["win"]])
            ratio = raw / 100 if raw > 1.5 else raw
            w_win = ratio * impressions

        out.append(
            {
                "date": date_iso,
                "campaign_id": cid,
                "campaign_name": cname,
                "project": resolve_row_project(sheet_project, cname),
                "direction": detect_direction(cname),
                "cost": cost,
                "clicks": clicks,
                "impressions": impressions,
                "w_avg_eff_bid": w_bid,
                "w_avg_traffic_vol": w_traffic,
                "w_avg_impr_pos": w_impr,
                "w_avg_click_pos": w_click,
                "w_auction_win_share": w_win,
            }
        )
    return out


def sync_direct_sheets() -> int:
    """Читает 4 листа Директа из GOOGLE_SHEETS_ID (полная история, как GAS)."""
    service = get_sheets_service()
    spreadsheet_id = os.environ["GOOGLE_SHEETS_ID"]
    all_rows: List[Dict[str, Any]] = []

    meta = (
        service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets.properties.title")
        .execute()
    )
    title_by_lower = {
        (s.get("properties", {}).get("title") or "").strip().lower(): (
            s.get("properties", {}).get("title") or ""
        ).strip()
        for s in meta.get("sheets", [])
        if s.get("properties", {}).get("title")
    }

    for project, sheet_name in DIRECT_SHEETS.items():
        actual_title = title_by_lower.get(sheet_name.strip().lower())
        if not actual_title:
            print(f"Директ листы: нет «{sheet_name}», пропуск")
            continue
        values = read_sheet(service, spreadsheet_id, actual_title)
        if len(values) < 2:
            print(f"Директ листы: «{sheet_name}» пустой")
            continue
        headers = [str(x) for x in values[0]]
        chunk = _parse_sheet(headers, values, project)
        print(f"Директ листы: «{actual_title}» ({project}) → {len(chunk)} строк")
        all_rows.extend(chunk)

    if not all_rows:
        return 0

    from sync.db import replace_direct_stats

    n = replace_direct_stats(all_rows)
    print(f"Директ листы: записано {n} строк в direct_stats")
    return n
