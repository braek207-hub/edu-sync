import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.lime_gcc import COLUMNS, merge_rows

COLS = ["date", "data_source", "region", "country", "channel", "subchannel", "traffic_type", "campaign_id",
        "campaign_name", "cost", "clicks", "impressions", "sessions", "users", "clients",
        "purchases_count", "purchases_revenue", "customers", "new_users", "new_customers", "new_customers_revenue"]


def test_merge_joins_by_channel_and_converts():
    metrika = [{"date": "2026-07-17", "traffic_source": "ad", "source_engine": "Google Ads", "visits": 500, "users": 400}]
    orders = [{"date": "2026-07-17", "channel": "SEM", "subchannel": "Google.Adwords", "traffic_type": "Платный", "orders": 10, "revenue": 1000.0}]
    spend = [{"date": "2026-07-17", "channel": "SEM", "subchannel": "Google.Adwords", "traffic_type": "Платный", "cost": 200.0}]
    rows = merge_rows(metrika, orders, spend, 20.0, "2026-07-17")
    assert len(rows) == 1
    r = dict(zip(COLS, rows[0]))
    assert r["region"] == "gcc" and r["data_source"] == "web"
    assert r["channel"] == "SEM" and r["subchannel"] == "Google.Adwords"
    assert r["sessions"] == 500 and r["users"] == 400
    assert r["purchases_count"] == 10
    assert r["purchases_revenue"] == 20000.0   # 1000 * 20
    assert r["cost"] == 4000.0                  # 200 * 20


def test_merge_traffic_only_channel():
    metrika = [{"date": "2026-07-17", "traffic_source": "direct", "source_engine": None, "visits": 30, "users": 25}]
    rows = merge_rows(metrika, [], [], 20.0, "2026-07-17")
    assert len(rows) == 1
    r = dict(zip(COLS, rows[0]))
    assert r["channel"] == "Direct" and r["sessions"] == 30 and r["purchases_count"] == 0 and r["cost"] == 0

# === Дробление по странам Залива (T5) ===


def test_columns_match_test_expectation():
    """COLS в тестах = реальный порядок колонок INSERT (иначе dict(zip(...)) врёт)."""
    assert list(COLUMNS) == COLS


def test_merge_splits_same_channel_by_country():
    metrika = [
        {"date": "2026-07-17", "country": "ОАЭ", "traffic_source": "ad",
         "source_engine": "Google Ads", "visits": 500, "users": 400},
        {"date": "2026-07-17", "country": "Катар", "traffic_source": "ad",
         "source_engine": "Google Ads", "visits": 50, "users": 40},
    ]
    orders = [
        {"date": "2026-07-17", "country": "ОАЭ", "channel": "SEM", "subchannel": "Google.Adwords",
         "traffic_type": "Платный", "orders": 10, "revenue": 1000.0},
        {"date": "2026-07-17", "country": "Катар", "channel": "SEM", "subchannel": "Google.Adwords",
         "traffic_type": "Платный", "orders": 1, "revenue": 300.0},
    ]
    rows = [dict(zip(COLS, r)) for r in merge_rows(metrika, orders, [], 20.0, "2026-07-17")]
    assert len(rows) == 2
    ae = [r for r in rows if r["country"] == "ОАЭ"][0]
    qa = [r for r in rows if r["country"] == "Катар"][0]
    assert ae["sessions"] == 500 and ae["purchases_count"] == 10 and ae["purchases_revenue"] == 20000.0
    assert qa["sessions"] == 50 and qa["purchases_count"] == 1 and qa["purchases_revenue"] == 6000.0
    assert all(r["channel"] == "SEM" for r in rows)


def test_merge_country_none_stays_separate_row():
    """Источник без гео-разбивки (расход Meta из TW summary) → country=None, идёт в GCC-тотал."""
    metrika = [{"date": "2026-07-17", "country": "ОАЭ", "traffic_source": "ad",
                "source_engine": "Instagram", "visits": 100, "users": 90}]
    spend = [{"date": "2026-07-17", "channel": "SMM paid", "subchannel": "Meta Ads",
              "traffic_type": "Платный", "cost": 500.0}]
    rows = [dict(zip(COLS, r)) for r in merge_rows(metrika, [], spend, 20.0, "2026-07-17")]
    assert len(rows) == 2
    ae = [r for r in rows if r["country"] == "ОАЭ"][0]
    total_only = [r for r in rows if r["country"] is None][0]
    assert ae["sessions"] == 100 and ae["cost"] == 0
    assert total_only["cost"] == 10000.0 and total_only["sessions"] == 0
    # GCC-тотал не пострадал: сумма по строкам = вся выручка/расход
    assert sum(r["cost"] for r in rows) == 10000.0


def test_merge_totals_preserved_across_countries():
    metrika = [
        {"date": "2026-07-17", "country": c, "traffic_source": "organic",
         "source_engine": "Google", "visits": v, "users": v}
        for c, v in (("ОАЭ", 300), ("Саудовская Аравия", 200), (None, 10))
    ]
    rows = [dict(zip(COLS, r)) for r in merge_rows(metrika, [], [], 20.0, "2026-07-17")]
    assert len(rows) == 3
    assert sum(r["sessions"] for r in rows) == 510
    assert {r["country"] for r in rows} == {"ОАЭ", "Саудовская Аравия", None}


def test_merge_backward_compatible_without_country_key():
    """Старые строки без ключа country (паритет) не падают — трактуются как тотал."""
    metrika = [{"date": "2026-07-17", "traffic_source": "direct", "source_engine": None,
                "visits": 30, "users": 25}]
    rows = [dict(zip(COLS, r)) for r in merge_rows(metrika, [], [], 20.0, "2026-07-17")]
    assert len(rows) == 1 and rows[0]["country"] is None and rows[0]["sessions"] == 30
