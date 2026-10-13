"""BJORN: поштучные покупки ложатся в ту же кампанию витрины, что и строка marketing_daily_fact."""
from __future__ import annotations

from sync.bjorn.build_fact import build_fact_rows
from sync.bjorn.sync_purchase_fact import build_purchase_fact_rows

PAID = "Переходы по рекламе"
DIRECT = [
    {
        "date": "2026-09-10",
        "campaign_id": "113890474",
        "campaign_name": "Поиск бренд",
        "source_type": "search",
        "impressions": 100,
        "clicks": 10,
        "cost_vat": 500.0,
    }
]


def stg(source_detail: str, utm: str, purchases: int, visits: int = 5) -> dict:
    return {
        "date": "2026-09-10",
        "attribution": "automatic",
        "source": PAID,
        "source_detail": source_detail,
        "utm_campaign": utm,
        "campaign_name": utm,
        "visits": visits,
        "add_to_cart": 0,
        "begin_checkout": 0,
        "purchases": purchases,
        "revenue": 1000.0 * purchases,
        "bounce_rate": None,
        "page_depth": None,
    }


def purchase(order_id: str, utm: str, source_detail: str = "Яндекс.Директ", date: str = "2026-09-10") -> dict:
    return {
        "order_id": order_id,
        "date": date,
        "attribution": "automatic",
        "source": PAID,
        "source_detail": source_detail,
        "utm_campaign": utm,
        "campaign_name": utm,
        "purchases": 1,
        "revenue": 1000.0,
    }


def test_second_row_of_a_campaign_keeps_its_direct_campaign():
    # Две строки одной кампании за день (разный «источник детально»): расход Директа — один
    # раз, но кампания у обеих своя. До 13.10.2026 вторая схлопывалась в «Не определено».
    rows = build_fact_rows([stg("Яндекс.Директ", "113890474", 1), stg("Яндекс", "113890474", 2)], DIRECT)
    assert [(r["source_detail"], r["campaign_id"], r["campaign_name"]) for r in rows] == [
        ("Яндекс.Директ", "113890474", "Поиск бренд"),
        ("Яндекс", "113890474", "Поиск бренд"),
    ]
    assert sum(r["cost_vat"] for r in rows) == 500.0
    assert sum(r["clicks"] for r in rows) == 10


def test_purchases_land_in_the_fact_campaign():
    fact = build_fact_rows([stg("Яндекс.Директ", "113890474", 1), stg("Яндекс.Директ", "999999999", 1)], DIRECT)
    purchases = build_purchase_fact_rows(
        [purchase("A1", "113890474"), purchase("A2", "999999999")], DIRECT
    )
    fact_keys = {(r["source_detail"], r["campaign_id"]) for r in fact if r["purchases"]}
    assert {(p["source_detail"], p["campaign_id"]) for p in purchases} == fact_keys
    # Номерная кампания без строки Директа — в безымянный узел, как в витрине.
    assert {p["order_id"]: p["campaign_id"] for p in purchases} == {"A1": "113890474", "A2": ""}
    assert purchases[0]["campaign_name"] == "Поиск бренд"
    assert purchases[0]["source_type"] == "search"


def test_one_row_per_purchase_and_model_earliest_wins():
    rows = build_purchase_fact_rows(
        [
            purchase("A1", "113890474", date="2026-09-11"),
            purchase("A1", "113890474", date="2026-09-10"),
            purchase("(not set)", "113890474"),
            {**purchase("A3", "113890474"), "purchases": 0},
        ],
        DIRECT,
    )
    assert [(r["order_id"], r["date"]) for r in rows] == [("A1", "2026-09-10")]


def test_direct_not_set_label_both_spellings():
    from sync.bjorn.common import derive_source_detail

    for raw in ("Яндекс.Директ: Не определено", "Яндекс Директ: Не определено"):
        assert derive_source_detail(raw, "", PAID) == "Яндекс.Директ"


def test_build_fact_refuses_window_without_staging():
    from sync.bjorn.build_fact import missing_staging_days

    rows = [{"date": "2026-05-21"}, {"date": "2026-05-23"}]
    assert missing_staging_days(rows, "2026-05-20", "2026-05-23") == ["2026-05-20", "2026-05-22"]
    assert missing_staging_days(rows + [{"date": "2026-05-22"}], "2026-05-21", "2026-05-23") == []
