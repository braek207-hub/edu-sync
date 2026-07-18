import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.gcc_triplewhale import order_source, aggregate_orders_by_channel, spend_by_channel


def _load(name):
    with open(os.path.join(os.path.dirname(__file__), "fixtures", name), encoding="utf-8") as f:
        return json.load(f)


def test_order_source_first_touchpoint():
    orders = _load("tw_orders_sample.json")["ordersWithJourneys"]
    assert order_source(orders[0]) == "facebook-ads"


def test_order_source_fallback_to_full_last_click():
    # заказ без lastPlatformClick/lastClick — источник берётся из fullLastClick
    order = {
        "attribution": {
            "lastPlatformClick": [],
            "lastClick": [],
            "fullLastClick": [{"source": "direct"}],
        }
    }
    assert order_source(order) == "direct"


def test_order_source_fallback_to_last_click():
    # заказ без lastPlatformClick — источник берётся из lastClick
    order = {
        "attribution": {
            "lastPlatformClick": [],
            "lastClick": [{"source": "google-ads"}],
            "fullLastClick": [{"source": "direct"}],
        }
    }
    assert order_source(order) == "google-ads"


def test_order_source_none_when_no_touchpoints():
    order = {"attribution": {"lastPlatformClick": [], "lastClick": [], "fullLastClick": []}}
    assert order_source(order) is None


def test_aggregate_orders_totals_match():
    orders = _load("tw_orders_sample.json")["ordersWithJourneys"]
    rows = aggregate_orders_by_channel(orders, "2026-07-17")
    assert sum(r["orders"] for r in rows) == len(orders)
    assert abs(sum(r["revenue"] for r in rows) - sum(float(o["total_price"]) for o in orders)) < 0.01
    # каждый row имеет дату и канал из таксономии
    assert all(r["date"] == "2026-07-17" and r["channel"] for r in rows)
    # facebook-ads заказ дал строку Meta
    assert any(r["channel"] == "SMM paid" and r["subchannel"] == "Meta Ads" for r in rows)


def test_spend_by_channel():
    spend = {m["metricId"]: m["values"]["current"] for m in _load("tw_spend_sample.json")["metrics"]}
    rows = spend_by_channel(spend, "2026-07-17")
    google = [r for r in rows if r["subchannel"] == "Google.Adwords"][0]
    assert round(google["cost"]) == 832
    meta = [r for r in rows if r["subchannel"] == "Meta Ads"][0]
    assert round(meta["cost"]) == 1301
    # нулевой snapchat пропущен
    assert not any(r["subchannel"] == "Snapchat Ads" for r in rows)
    # все строки платного канала, с датой
    assert all(r["date"] == "2026-07-17" and r["traffic_type"] == "Платный" for r in rows)
