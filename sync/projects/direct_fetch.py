"""Выгрузка Директа по логину клиента: расход, клики, показы по кампаниям и дням."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from ..bjorn.common import clean_text, money, parse_int
from ..bjorn.sync_direct import fetch_report

FIELDS = ["Date", "CampaignId", "CampaignName", "Impressions", "Clicks", "Cost"]


@dataclass(frozen=True)
class DirectRow:
    date: str
    campaign_id: int
    campaign_name: str
    cost: float
    clicks: int
    impressions: int


def fetch_direct_daily(
    login: str, token: str, date_from: str, date_to: str, *, max_wait_sec: float
) -> list[DirectRow]:
    text = fetch_report(login, date_from, date_to, FIELDS, token=token, max_wait_sec=max_wait_sec)
    agg: dict[tuple[str, int], dict] = {}
    for row in csv.DictReader(io.StringIO(text), delimiter="\t"):
        row_date = clean_text(row.get("Date"))
        raw_id = clean_text(row.get("CampaignId"))
        # Итоговая строка отчёта и пустые id: «--» в CampaignId.
        if not row_date or not raw_id.isdigit():
            continue
        item = agg.setdefault(
            (row_date, int(raw_id)), {"name": "", "cost": 0.0, "clicks": 0, "impressions": 0}
        )
        name = clean_text(row.get("CampaignName"))
        if name:
            item["name"] = name
        item["cost"] += money(row.get("Cost"))
        item["clicks"] += parse_int(row.get("Clicks"))
        item["impressions"] += parse_int(row.get("Impressions"))
    return [
        DirectRow(d, cid, v["name"], round(v["cost"], 2), v["clicks"], v["impressions"])
        for (d, cid), v in sorted(agg.items())
    ]
