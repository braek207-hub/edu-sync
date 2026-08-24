import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

sys.modules.setdefault("googleapiclient", MagicMock())
sys.modules.setdefault("googleapiclient.discovery", MagicMock())

from sync.crm import _sync_leads_raw, merge_leads_agg

HEADERS = [
    "ID", "date created", "UTM Campaign", "Ленд",
    "Город (IP)", "Б24 год выпуска", "Б24 уровень образования", "Родитель",
    "connect", "Сделка", "Этап",
]

PAID = {"L1": {"count": 1, "revenue": 100000.0, "pay_date": "2026-07-25"}}


def _row(lead_id, day="20.07.2026"):
    return [lead_id, day, "705494889", "vuz_edunetwork",
            "Москва", "2008", "11 класс", "Да", "1", "1", "Сделка создана"]


def _totals(agg):
    return (
        sum(b["leads"] for b in agg.values()),
        sum(b["payments_from_leads"] for b in agg.values()),
        sum(b["revenue_from_leads"] for b in agg.values()),
    )


def test_duplicate_rows_in_one_sheet_counted_once():
    values = [HEADERS, _row("L1"), _row("L1"), _row("L2")]
    agg, _dims, details = _sync_leads_raw(HEADERS, values, PAID)
    assert _totals(agg) == (2, 1, 100000.0)
    assert [d["lead_id"] for d in details] == ["L1", "L2"]


def test_same_lead_in_two_sheets_counted_once():
    """Лид, попавший и в «Лиды», и в «Лиды 2025», не удваивает витрину."""
    seen: set[str] = set()
    agg_a, _d, det_a = _sync_leads_raw(HEADERS, [HEADERS, _row("L1"), _row("L2")], PAID, seen)
    agg_b, _d, det_b = _sync_leads_raw(HEADERS, [HEADERS, _row("L1"), _row("L3")], PAID, seen)
    merged: dict = {}
    merge_leads_agg(merged, agg_a)
    merge_leads_agg(merged, agg_b)
    assert _totals(merged) == (3, 1, 100000.0)
    assert [d["lead_id"] for d in det_a + det_b] == ["L1", "L2", "L3"]


def test_rows_without_id_are_kept():
    """Дедуплицировать нечем — считаем все: иначе теряем лиды без id."""
    values = [HEADERS, _row(""), _row(""), _row("L1")]
    agg, _dims, _details = _sync_leads_raw(HEADERS, values, PAID)
    assert _totals(agg)[0] == 3
