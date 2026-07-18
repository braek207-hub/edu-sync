import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.lime_gcc import merge_rows

COLS = ["date", "data_source", "region", "channel", "subchannel", "traffic_type", "campaign_id",
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