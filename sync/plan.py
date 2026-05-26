"""Синк plan_monthly из Google Sheets → monthly_plans (как GAS readPlanMonthly_)."""

from __future__ import annotations

import os
import re
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Tuple

from sync.sheets import get_sheets_service, read_sheet
from sync.utils import pick_index_loose, to_num

PLAN_SHEET = "plan_monthly"

# Как ensurePlanMonthlySheet_ в GAS
STANDARD_PLAN_HEADERS = [
    "month",
    "project",
    "direction",
    "plan_budget_vat",
    "plan_leads",
    "plan_connections",
    "plan_deals",
    "plan_sales",
    "plan_revenue",
    "notes",
]


def _plan_column_indices(headers: List[str]) -> Dict[str, int]:
    """Индексы колонок: сначала точное совпадение заголовка, иначе pick_index_loose."""
    h = [str(x or "").strip().lower() for x in headers]
    h_note = list(h)
    if len(h_note) >= 10 and h_note[9] in ("note", "notes"):
        h_note[9] = "notes"
    if h_note[: len(STANDARD_PLAN_HEADERS)] == STANDARD_PLAN_HEADERS and len(h) >= 4:
        return {
            "month": 0,
            "project": 1,
            "direction": 2,
            "budget": 3,
            "leads": 4,
            "connections": 5,
            "deals": 6,
            "payments": 7,
            "revenue": 8,
        }
    return {
        "month": pick_index_loose(headers, ["month", "месяц"]),
        "project": pick_index_loose(headers, ["project", "проект"]),
        "direction": pick_index_loose(headers, ["direction", "направ"]),
        "budget": pick_index_loose(
            headers, ["plan_budget_vat", "budget", "план бюджет", "план_бюджет"]
        ),
        "leads": pick_index_loose(headers, ["plan_leads", "leads", "план лид"]),
        "connections": pick_index_loose(
            headers, ["plan_connections", "соединения", "connections"]
        ),
        "deals": pick_index_loose(headers, ["plan_deals", "сделки", "deals"]),
        "payments": pick_index_loose(
            headers, ["plan_sales", "sales", "план продаж", "план_продаж"]
        ),
        "revenue": pick_index_loose(
            headers, ["plan_revenue", "revenue", "план выручка", "план_выручка"]
        ),
    }


def normalize_month_key(v: Any) -> str:
    if v is None or v == "":
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m")
    s = str(v).strip()
    if not s:
        return ""
    m1 = re.match(r"^(\d{4})-(\d{2})(?:-\d{2})?$", s)
    if m1:
        return f"{m1.group(1)}-{m1.group(2)}"
    m2 = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})", s)
    if m2:
        return f"{m2.group(3)}-{m2.group(2)}"
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.strftime("%Y-%m")
    except (ValueError, TypeError):
        return ""


def sync_plan_monthly() -> int:
    service = get_sheets_service()
    spreadsheet_id = os.environ["GOOGLE_SHEETS_ID"]
    values = read_sheet(
        service,
        spreadsheet_id,
        PLAN_SHEET,
        value_render_option="UNFORMATTED_VALUE",
    )
    if len(values) < 2:
        print("План: пустой лист plan_monthly")
        return 0

    headers = [str(x).strip().lower() for x in values[0]]
    pi = _plan_column_indices(headers)
    if pi["month"] == -1:
        raise ValueError("План: не найдена колонка month")
    print(
        "План: колонки "
        f"budget={pi['budget']} leads={pi['leads']} "
        f"connections={pi['connections']} deals={pi['deals']} "
        f"sales={pi['payments']} revenue={pi['revenue']}"
    )

    agg: Dict[Tuple[str, str, str], Dict[str, Any]] = defaultdict(
        lambda: {
            "budget": 0.0,
            "leads": 0,
            "connections": 0,
            "deals": 0,
            "payments": 0,
            "revenue": 0.0,
        }
    )
    skipped_month = 0
    raw_rows = 0

    for r in values[1:]:
        raw_rows += 1
        ym = normalize_month_key(r[pi["month"]] if pi["month"] < len(r) else "")
        if not ym:
            skipped_month += 1
            continue
        project = (
            str(r[pi["project"]]).strip().lower()
            if pi["project"] != -1 and pi["project"] < len(r)
            else ""
        )
        direction = (
            str(r[pi["direction"]]).strip().lower()
            if pi["direction"] != -1 and pi["direction"] < len(r)
            else ""
        )
        key = (f"{ym}-01", project, direction)
        bucket = agg[key]
        bucket["budget"] += to_num(
            r[pi["budget"]] if pi["budget"] != -1 and pi["budget"] < len(r) else 0
        )
        bucket["leads"] += int(
            to_num(r[pi["leads"]] if pi["leads"] != -1 and pi["leads"] < len(r) else 0)
        )
        bucket["connections"] += int(
            to_num(
                r[pi["connections"]]
                if pi["connections"] != -1 and pi["connections"] < len(r)
                else 0
            )
        )
        bucket["deals"] += int(
            to_num(r[pi["deals"]] if pi["deals"] != -1 and pi["deals"] < len(r) else 0)
        )
        bucket["payments"] += int(
            to_num(
                r[pi["payments"]]
                if pi["payments"] != -1 and pi["payments"] < len(r)
                else 0
            )
        )
        bucket["revenue"] += to_num(
            r[pi["revenue"]] if pi["revenue"] != -1 and pi["revenue"] < len(r) else 0
        )

    rows: List[Dict[str, Any]] = []
    for (month, project, direction), b in agg.items():
        rows.append(
            {
                "month": month,
                "project": project,
                "direction": direction,
                "budget": round(b["budget"], 2),
                "leads": b["leads"],
                "connections": b["connections"],
                "deals": b["deals"],
                "payments": b["payments"],
                "revenue": round(b["revenue"], 2),
            }
        )

    zero_budget = sum(1 for r in rows if r["budget"] == 0)
    zero_leads = sum(1 for r in rows if r["leads"] == 0)
    by_proj: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"budget": 0.0, "leads": 0}
    )
    for r in rows:
        by_proj[r["project"]]["budget"] += r["budget"]
        by_proj[r["project"]]["leads"] += r["leads"]
    print(
        f"План: лист {raw_rows} строк → {len(rows)} ключей "
        f"(пропуск month: {skipped_month}, budget=0: {zero_budget}, leads=0: {zero_leads})"
    )
    for proj in sorted(by_proj.keys()):
        t = by_proj[proj]
        print(f"  Σ {proj}: budget={t['budget']:.0f}, leads={t['leads']:.0f}")
    from sync.db import upsert_monthly_plans

    n = upsert_monthly_plans(rows)
    print(f"План: upsert {n} строк в monthly_plans")
    return n
