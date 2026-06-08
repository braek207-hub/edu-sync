"""Построчные CRM для extended/Sales — порт GAS readCrmLeadsLite / readCrmPaymentsLite."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List

from sync.classify import (
    detect_direction,
    map_crm_land,
    normalize_b24_dim,
    normalize_city_ip_segment,
)
from sync.crm import _cell, crm_leads_sheets, crm_payments_sheets
from sync.sheets import get_sheets_service, read_sheet
from sync.utils import normalize_campaign_id, pick_index_loose, to_datetime_ms, to_iso_date, to_num


def _meta_from_direct_stats() -> Dict[str, Dict[str, str]]:
    """campaign_id -> {project, direction, campaign_name} из последнего синка Direct."""
    from sync.db import get_connection

    meta: Dict[str, Dict[str, str]] = {}
    sql = """
        SELECT DISTINCT ON (campaign_id)
            campaign_id, campaign_name, project, direction
        FROM direct_stats
        ORDER BY campaign_id, date DESC
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            for cid, cname, proj, direc in cur.fetchall():
                meta[str(cid)] = {
                    "campaign_name": str(cname or ""),
                    "project": str(proj or "unknown"),
                    "direction": str(direc or "other"),
                }
    return meta


def _read_leads_lite(
    headers: List[str], values: List[List[Any]], meta: Dict[str, Dict[str, str]]
) -> List[Dict[str, Any]]:
    li = {
        "id": pick_index_loose(headers, ["id"]),
        "date": pick_index_loose(headers, ["date created", "дата создания"]),
        "land": pick_index_loose(headers, ["ленд", "land"]),
        "campaign": pick_index_loose(headers, ["utm campaign", "utm_campaign", "campaign"]),
        "source": pick_index_loose(headers, ["б24 источник", "source", "utm source"]),
        "responsible": pick_index_loose(headers, ["ответственный"]),
        "dispatcher": pick_index_loose(
            headers, ["диспетчер", "dispatcher", "дисп", "диспетчер фио"]
        ),
        "connections": pick_index_loose(headers, ["connect"]),
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
        "deals": pick_index_loose(headers, ["сделка", "сделки"]),
        "time_to_connect": pick_index_loose(
            headers,
            ["время до соединения", "time to connect", "lag to connect", "t2connect"],
        ),
        "city_ip": pick_index_loose(headers, ["город (ip)", "город(ip)", "город ip"]),
        "grad_year": pick_index_loose(headers, ["б24 год выпуска", "год выпуска"]),
        "edu_level": pick_index_loose(
            headers,
            ["б24 уровень образования", "уровень образования", "класс/курс"],
        ),
    }
    if li["date"] == -1:
        return []

    if len(headers) > 7 and li["dispatcher"] == -1:
        h7 = str(headers[7]).lower()
        if "диспетч" in h7:
            li["dispatcher"] = 7

    out: List[Dict[str, Any]] = []
    for row in values[1:]:
        date_iso = to_iso_date(_cell(row, li["date"]))
        if not date_iso:
            continue
        created_at_ms = to_datetime_ms(_cell(row, li["date"]))
        connected_at_ms = (
            to_datetime_ms(_cell(row, li["date_connect"]))
            if li["date_connect"] != -1
            else None
        )
        cid = normalize_campaign_id(_cell(row, li["campaign"]))
        land = str(_cell(row, li["land"])).strip().lower() if li["land"] != -1 else ""
        m = meta.get(cid) if cid else None
        project = (
            m["project"]
            if m
            else (map_crm_land(land) if land else "unknown")
        )
        direction = m["direction"] if m else detect_direction(_cell(row, li["campaign"]))

        conn = 0.0
        if li["connections"] != -1:
            conn = to_num(_cell(row, li["connections"]))
        elif li["date_connect"] != -1 and str(_cell(row, li["date_connect"]) or "").strip():
            conn = 1.0

        time_to_connect = 0
        if (
            created_at_ms is not None
            and connected_at_ms is not None
            and connected_at_ms >= created_at_ms
        ):
            time_to_connect = int(round((connected_at_ms - created_at_ms) / 1000))
        elif li["time_to_connect"] != -1:
            time_to_connect = int(to_num(_cell(row, li["time_to_connect"])))

        cname = m["campaign_name"] if m else ""
        out.append(
            {
                "id": str(_cell(row, li["id"])) if li["id"] != -1 else "",
                "date": date_iso,
                "campaignId": cid,
                "campaignName": cname,
                "project": project,
                "direction": direction,
                "source": str(_cell(row, li["source"])) if li["source"] != -1 else "",
                "responsible": (
                    str(_cell(row, li["responsible"])) if li["responsible"] != -1 else ""
                ),
                "dispatcher": (
                    str(_cell(row, li["dispatcher"])) if li["dispatcher"] != -1 else ""
                ),
                "cityIpSegment": (
                    normalize_city_ip_segment(_cell(row, li["city_ip"]))
                    if li["city_ip"] != -1
                    else "rf"
                ),
                "b24GradYear": (
                    normalize_b24_dim(_cell(row, li["grad_year"]))
                    if li["grad_year"] != -1
                    else "unknown"
                ),
                "b24EduLevel": (
                    normalize_b24_dim(_cell(row, li["edu_level"]))
                    if li["edu_level"] != -1
                    else "unknown"
                ),
                "connections": conn,
                "connectedAt": (
                    to_iso_date(_cell(row, li["date_connect"]))
                    if li["date_connect"] != -1
                    else ""
                ),
                "connectedAtMs": connected_at_ms if connected_at_ms is not None else 0,
                "deals": to_num(_cell(row, li["deals"])) if li["deals"] != -1 else 0,
                "timeToConnect": time_to_connect,
            }
        )
    return out


def _read_payments_lite(
    headers: List[str], values: List[List[Any]], meta: Dict[str, Dict[str, str]]
) -> List[Dict[str, Any]]:
    pi = {
        "lead_id": pick_index_loose(headers, ["id лида в scrm", "lead id", "id лида"]),
        "pay_date": pick_index_loose(headers, ["date pay", "дата оплаты"]),
        "revenue": pick_index_loose(headers, ["выручка", "revenue", "сумма"]),
        "orders": pick_index_loose(headers, ["orders", "оплат"]),
        "source": pick_index_loose(headers, ["источник (utm source)", "utm source"]),
        "product": pick_index_loose(headers, ["группа продуктов"]),
        "campaign": pick_index_loose(
            headers, ["кампания (utm campaign)", "utm campaign", "кампания"]
        ),
        "responsible": pick_index_loose(headers, ["ответственный", "ответственные"]),
        "land": pick_index_loose(headers, ["ленд", "land"]),
    }
    if len(headers) > 17 and pi["revenue"] == -1:
        if headers[17] and re.search(r"выруч|revenue|сумма", str(headers[17]), re.I):
            pi["revenue"] = 17
    if len(headers) > 18 and pi["orders"] == -1:
        if headers[18] and re.search(r"orders|оплат", str(headers[18]), re.I):
            pi["orders"] = 18
    if pi["pay_date"] == -1:
        return []

    out: List[Dict[str, Any]] = []
    for row in values[1:]:
        date_pay = to_iso_date(_cell(row, pi["pay_date"]))
        if not date_pay:
            continue
        cid = normalize_campaign_id(_cell(row, pi["campaign"])) if pi["campaign"] != -1 else ""
        land = str(_cell(row, pi["land"])).strip().lower() if pi["land"] != -1 else ""
        m = meta.get(cid) if cid else None
        orders_val = 0
        if pi["orders"] != -1:
            raw = _cell(row, pi["orders"])
            n = to_num(raw)
            s = str(raw or "").strip().lower()
            orders_val = 1 if round(n) == 1 or s in ("1", "1.0", "true", "да") else 0

        out.append(
            {
                "leadId": str(_cell(row, pi["lead_id"])) if pi["lead_id"] != -1 else "",
                "datePay": date_pay,
                "campaignId": cid,
                "project": (
                    m["project"]
                    if m
                    else (map_crm_land(land) if land else "unknown")
                ),
                "direction": m["direction"] if m else detect_direction(""),
                "source": str(_cell(row, pi["source"])) if pi["source"] != -1 else "",
                "responsible": (
                    str(_cell(row, pi["responsible"])) if pi["responsible"] != -1 else ""
                ),
                "productGroup": str(_cell(row, pi["product"])) if pi["product"] != -1 else "",
                "revenue": to_num(_cell(row, pi["revenue"])) if pi["revenue"] != -1 else 0,
                "orders": orders_val,
            }
        )
    return out


def sync_crm_lite() -> int:
    service = get_sheets_service()
    spreadsheet_id = os.environ["GOOGLE_SHEETS_ID"]
    meta = _meta_from_direct_stats()

    leads_lite: List[Dict[str, Any]] = []
    for sheet_name in crm_leads_sheets():
        try:
            leads_vals = read_sheet(service, spreadsheet_id, sheet_name)
        except Exception as e:
            print(f"CRM lite [{sheet_name}]: ошибка чтения лидов — {e}")
            continue
        if len(leads_vals) < 2:
            continue
        chunk = _read_leads_lite([str(x) for x in leads_vals[0]], leads_vals, meta)
        print(f"CRM lite [{sheet_name}]: {len(chunk)} лидов")
        leads_lite.extend(chunk)

    payments_lite: List[Dict[str, Any]] = []
    for sheet_name in crm_payments_sheets():
        try:
            pay_vals = read_sheet(service, spreadsheet_id, sheet_name)
        except Exception as e:
            print(f"CRM lite [{sheet_name}]: ошибка чтения оплат — {e}")
            continue
        if len(pay_vals) < 2:
            continue
        chunk = _read_payments_lite([str(x) for x in pay_vals[0]], pay_vals, meta)
        print(f"CRM lite [{sheet_name}]: {len(chunk)} оплат")
        payments_lite.extend(chunk)

    from sync.db import upsert_dashboard_extras

    n = upsert_dashboard_extras(
        json.dumps(leads_lite, ensure_ascii=False),
        json.dumps(payments_lite, ensure_ascii=False),
    )
    print(f"CRM lite: {len(leads_lite)} лидов, {len(payments_lite)} оплат → dashboard_extras")
    return n
