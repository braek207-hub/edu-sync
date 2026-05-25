import os
import re
from collections import defaultdict
from typing import Any, Dict, List

from sync.classify import detect_direction, detect_project, map_crm_land
from sync.sheets import get_sheets_service, read_sheet
from sync.utils import normalize_campaign_id, pick_index_loose, to_iso_date, to_num

CRM_LEADS_SHEET = "Лиды"
CRM_PAYMENTS_SHEET = "Оплаты"


def _cell(row: List[Any], idx: int) -> Any:
    if idx == -1 or idx >= len(row):
        return ""
    return row[idx]


def _apply_meta(bucket: Dict[str, Any], raw_campaign: Any) -> None:
    name = str(raw_campaign or "").strip()
    if name and not bucket.get("campaign_name"):
        bucket["campaign_name"] = name
        bucket["project"] = detect_project(name)
        bucket["direction"] = detect_direction(name)


def _log_spreadsheet_tabs(service, spreadsheet_id: str) -> None:
    meta = (
        service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets.properties.title")
        .execute()
    )
    titles = [
        s["properties"]["title"]
        for s in meta.get("sheets", [])
        if s.get("properties", {}).get("title")
    ]
    print(f"CRM: листы в книге ({len(titles)}): {', '.join(titles[:20])}")


def _sync_leads_raw(headers: List[str], values: List[List[Any]]) -> Dict[str, Dict[str, Any]]:
    """Построчные лиды (как GAS): date created + utm campaign."""
    li = {
        "date": pick_index_loose(
            headers, ["date created", "дата создания", "дата", "date"]
        ),
        "campaign": pick_index_loose(
            headers,
            [
                "utm campaign",
                "utm_campaign",
                "utmcampaign",
                "campaign",
                "кампания",
            ],
        ),
        "land": pick_index_loose(headers, ["ленд", "land"]),
        "connections": pick_index_loose(
            headers, ["connect", "количество соединений"]
        ),
        "date_connect": pick_index_loose(
            headers,
            [
                "б24 дата соединения",
                "дата соединения",
                "date connect",
                "connect date",
                "дата коннекта",
                "дата дозвона",
            ],
        ),
        "deals": pick_index_loose(
            headers, ["уникальные сделки", "сделка", "сделки", "deals", "deal"]
        ),
    }
    if li["date"] == -1:
        raise ValueError("CRM(Лиды): не найдена колонка даты")
    if li["campaign"] == -1:
        raise ValueError("CRM(Лиды): не найдена колонка utm campaign")

    agg: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "leads": 0,
            "connections": 0.0,
            "deals": 0.0,
            "project": "unknown",
            "direction": "other",
            "campaign_name": "",
        }
    )

    for row in values[1:]:
        date_iso = to_iso_date(_cell(row, li["date"]))
        if not date_iso:
            continue
        cid = normalize_campaign_id(_cell(row, li["campaign"]))
        land = (
            str(_cell(row, li["land"])).strip().lower() if li["land"] != -1 else ""
        )
        if not cid and land:
            cid = f"land:{land}"
        if not cid:
            continue
        key = f"{date_iso}|{cid}"
        bucket = agg[key]
        _apply_meta(bucket, _cell(row, li["campaign"]))
        if land and bucket.get("project") == "unknown":
            bucket["project"] = map_crm_land(land)
        bucket["leads"] += 1
        if li["connections"] != -1:
            bucket["connections"] += to_num(_cell(row, li["connections"]))
        elif li["date_connect"] != -1:
            if str(_cell(row, li["date_connect"]) or "").strip() not in ("", "0"):
                bucket["connections"] += 1
        if li["deals"] != -1:
            bucket["deals"] += to_num(_cell(row, li["deals"]))
    return agg


def _sync_leads_by_land(headers: List[str], values: List[List[Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Агрегат по дате + ленду (колонки Дата / Ленд / Лиды / Соединения) —
    как на сводном листе в книге, не построчный CRM.
    """
    li = {
        "date": pick_index_loose(headers, ["дата", "date"]),
        "land": pick_index_loose(headers, ["ленд", "land", "проект"]),
        "leads": pick_index_loose(headers, ["лиды", "leads"]),
        "connections": pick_index_loose(headers, ["соединен", "connect"]),
        "deals": pick_index_loose(headers, ["уникальные сделки", "сделки", "deals"]),
    }
    if li["date"] == -1 or li["land"] == -1 or li["leads"] == -1:
        return {}

    agg: Dict[str, Dict[str, Any]] = {}
    for row in values[1:]:
        date_iso = to_iso_date(_cell(row, li["date"]))
        land = str(_cell(row, li["land"])).strip().lower()
        if not date_iso or not land:
            continue
        cid = f"land:{land}"
        key = f"{date_iso}|{cid}"
        agg[key] = {
            "leads": int(to_num(_cell(row, li["leads"]))),
            "connections": int(
                to_num(_cell(row, li["connections"])) if li["connections"] != -1 else 0
            ),
            "deals": int(to_num(_cell(row, li["deals"])) if li["deals"] != -1 else 0),
            "project": map_crm_land(land) if land else "unknown",
            "direction": detect_direction(land),
            "campaign_name": land,
        }
    return agg


def sync_crm_leads() -> int:
    service = get_sheets_service()
    spreadsheet_id = os.environ["GOOGLE_SHEETS_ID"]

    values = read_sheet(service, spreadsheet_id, CRM_LEADS_SHEET)
    sheet_used = CRM_LEADS_SHEET

    if len(values) < 2:
        meta = (
            service.spreadsheets()
            .get(spreadsheetId=spreadsheet_id, fields="sheets.properties.title")
            .execute()
        )
        for s in meta.get("sheets", []):
            title = s.get("properties", {}).get("title") or ""
            if not title or title == CRM_LEADS_SHEET:
                continue
            candidate = read_sheet(service, spreadsheet_id, title)
            if len(candidate) < 2:
                continue
            hlow = " ".join(str(x).lower() for x in candidate[0])
            if "ленд" in hlow and "лиды" in hlow and "дата" in hlow:
                values = candidate
                sheet_used = title
                print(f"CRM Лиды: используем лист «{title}» (свод Дата+Ленд)")
                break
        if len(values) < 2:
            print("CRM Лиды: пустой лист или только заголовок")
            _log_spreadsheet_tabs(service, spreadsheet_id)
            return 0

    headers = [str(x) for x in values[0]]
    print(f"CRM Лиды [{sheet_used}]: заголовки = {headers[:12]}")

    agg: Dict[str, Dict[str, Any]] = {}
    try:
        agg = _sync_leads_raw(headers, values)
        if agg:
            print("CRM Лиды: режим построчный (utm campaign)")
    except ValueError:
        pass

    if not agg:
        agg = _sync_leads_by_land(headers, values)
        if agg:
            print("CRM Лиды: режим свод по Ленд (Дата + Ленд + Лиды)")

    if not agg:
        _log_spreadsheet_tabs(service, spreadsheet_id)
        raise ValueError(f"CRM(Лиды): нет строк после разбора, заголовки: {headers[:15]}")

    try:
        from sync.crm_lite import _meta_from_direct_stats

        meta = _meta_from_direct_stats()
        for key, bucket in agg.items():
            _cid = key.split("|", 1)[1]
            m = meta.get(_cid)
            if not m:
                continue
            if m.get("project") and m["project"] != "unknown":
                bucket["project"] = m["project"]
            if m.get("direction") and m["direction"] != "other":
                bucket["direction"] = m["direction"]
    except Exception as e:
        print(f"CRM Лиды: meta Direct пропущена: {e}")

    rows = []
    for key, v in agg.items():
        date_iso, campaign_id = key.split("|", 1)
        rows.append(
            {
                "date": date_iso,
                "campaign_id": campaign_id,
                "project": v["project"],
                "direction": v["direction"],
                "leads": int(v["leads"]),
                "connections": int(v["connections"]),
                "deals": int(v["deals"]),
            }
        )

    print(f"CRM Лиды: {len(rows)} агрегированных строк")
    from sync.db import upsert_crm_leads

    n = upsert_crm_leads(rows)
    print(f"CRM Лиды: upsert {n} строк в crm_leads")
    return n


def sync_crm_payments() -> int:
    service = get_sheets_service()
    spreadsheet_id = os.environ["GOOGLE_SHEETS_ID"]

    values = read_sheet(service, spreadsheet_id, CRM_PAYMENTS_SHEET)
    if len(values) < 2:
        print("CRM Оплаты: пустой лист")
        return 0

    headers = [str(x) for x in values[0]]
    pi = {
        "pay_date": pick_index_loose(headers, ["date pay", "дата оплаты"]),
        "campaign": pick_index_loose(
            headers, ["кампания", "utm campaign", "utm_campaign", "utm_campaign"]
        ),
        "revenue": pick_index_loose(headers, ["выручка", "revenue", "сумма", "оборот"]),
        "orders": pick_index_loose(headers, ["orders", "оплат"]),
    }
    if len(headers) > 17 and re_match_revenue(headers[17]):
        pi["revenue"] = 17
    if len(headers) > 18 and re_match_orders(headers[18]):
        pi["orders"] = 18

    if pi["pay_date"] == -1:
        raise ValueError('CRM(Оплаты): не найдена колонка "date pay"')
    if pi["campaign"] == -1:
        raise ValueError("CRM(Оплаты): не найдена колонка campaign")
    if pi["revenue"] == -1:
        raise ValueError('CRM(Оплаты): не найдена колонка "Выручка"')

    agg: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "payments": 0,
            "revenue": 0.0,
            "project": "unknown",
            "direction": "other",
            "campaign_name": "",
        }
    )

    for row in values[1:]:
        date_iso = to_iso_date(_cell(row, pi["pay_date"]))
        if not date_iso:
            continue
        cid = normalize_campaign_id(_cell(row, pi["campaign"]))
        if not cid:
            continue
        key = f"{date_iso}|{cid}"
        bucket = agg[key]
        _apply_meta(bucket, _cell(row, pi["campaign"]))

        if pi["orders"] != -1:
            p_num = to_num(_cell(row, pi["orders"]))
            p_bin = 1 if round(p_num) == 1 else 0
            bucket["payments"] += p_bin
        else:
            bucket["payments"] += 1
        bucket["revenue"] += to_num(_cell(row, pi["revenue"]))

    rows = []
    for key, v in agg.items():
        date_iso, campaign_id = key.split("|", 1)
        rows.append(
            {
                "date": date_iso,
                "campaign_id": campaign_id,
                "project": v["project"],
                "direction": v["direction"],
                "payments": int(v["payments"]),
                "revenue": round(float(v["revenue"]), 2),
            }
        )

    print(f"CRM Оплаты: {len(rows)} агрегированных строк")
    from sync.db import upsert_crm_payments

    n = upsert_crm_payments(rows)
    print(f"CRM Оплаты: upsert {n} строк в crm_payments")
    return n


def re_match_revenue(header: str) -> bool:
    return bool(re.search(r"выруч|revenue|сумма", str(header), re.I))


def re_match_orders(header: str) -> bool:
    return bool(re.search(r"orders|оплат", str(header), re.I))
