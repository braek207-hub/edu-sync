import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.gcc_triplewhale import (
    aggregate_orders_by_channel,
    order_country,
    order_source,
    spend_by_channel,
)


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


# === Страна заказа из journey (зонд P3) ===


def test_order_country_from_freshest_touchpoint():
    """journey отсортирован по убыванию времени → страна = первый тачпоинт с path."""
    orders = _load("tw_orders_journey_sample.json")["ordersWithJourneys"]
    countries = [order_country(o) for o in orders]
    assert countries.count("ОАЭ") == 2
    assert countries.count("Саудовская Аравия") == 2
    assert countries.count("Кувейт") == 1
    # заказы без journey (нет пиксель-данных) → страна не определена
    assert countries.count(None) == 2


def test_order_country_skips_events_without_path():
    """add2c не несёт path — пропускаем и берём следующий тачпоинт."""
    order = {"journey": [
        {"time": "2026-07-17T12:00:00+04:00", "event": "add2c", "productId": 1},
        {"time": "2026-07-17T11:59:00+04:00", "event": "page loaded",
         "path": "https://qa.limestore.com/products/x"},
    ]}
    assert order_country(order) == "Катар"


def test_order_country_ignores_non_gcc_hosts():
    order = {"journey": [
        {"time": "1", "event": "page loaded", "path": "https://www.limestore.com/"},
        {"time": "0", "event": "page loaded", "path": "https://om.lime-shop.com/cart"},
    ]}
    assert order_country(order) == "Оман"


def test_order_country_empty_journey():
    assert order_country({}) is None
    assert order_country({"journey": []}) is None


def test_aggregate_orders_splits_by_country():
    orders = _load("tw_orders_journey_sample.json")["ordersWithJourneys"]
    rows = aggregate_orders_by_channel(orders, "2026-07-17")
    # тотал не поехал: сумма по строкам = сумма по заказам
    assert sum(r["orders"] for r in rows) == len(orders)
    assert abs(sum(r["revenue"] for r in rows)
               - sum(float(o["total_price"]) for o in orders)) < 0.01
    # страна попала в ключ агрегации
    assert {r["country"] for r in rows} == {"ОАЭ", "Саудовская Аравия", "Кувейт", None}
    sa = [r for r in rows if r["country"] == "Саудовская Аравия"]
    assert sum(r["orders"] for r in sa) == 2
    assert abs(sum(r["revenue"] for r in sa) - (1737 + 1306)) < 0.01


def test_aggregate_orders_same_channel_different_countries_stay_split():
    orders = [
        {"total_price": 100, "attribution": {"lastPlatformClick": [{"source": "google-ads"}]},
         "journey": [{"event": "page loaded", "path": "https://ae.limestore.com/"}]},
        {"total_price": 300, "attribution": {"lastPlatformClick": [{"source": "google-ads"}]},
         "journey": [{"event": "page loaded", "path": "https://sa.limestore.com/"}]},
    ]
    rows = aggregate_orders_by_channel(orders, "2026-07-17")
    assert len(rows) == 2
    assert {(r["country"], r["revenue"]) for r in rows} == {("ОАЭ", 100.0), ("Саудовская Аравия", 300.0)}
    assert all(r["subchannel"] == "Google.Adwords" for r in rows)
