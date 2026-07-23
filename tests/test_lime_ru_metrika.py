# -*- coding: utf-8 -*-
"""Тесты свёртки RU-среза Метрики (sync/lime_ru_metrika.build_rows)."""
from sync.lime_ru_metrika import COLUMNS, build_rows


def _col(row, name):
    return row[COLUMNS.index(name)]


def test_aggregates_by_channel_campaign_weighted_bounce():
    # Две строки Метрики одной кампании (Директ) в день: должны свернуться в одну,
    # bounce/depth взвешены по визитам.
    metrika = [
        {"traffic_source": "ad", "source_engine": "Yandex.Direct",
         "utm_campaign": "709091521", "direct_campaign_name": "Медийка Баннеры",
         "visits": 100, "users": 90, "new_users": 40,
         "bounce_rate": 30.0, "page_depth": 2.0,
         "cart_reaches": 5, "checkout_reaches": 2, "orders": 1, "revenue": 5000.0},
        {"traffic_source": "ad", "source_engine": "Yandex.Direct",
         "utm_campaign": "709091521", "direct_campaign_name": "Медийка Баннеры",
         "visits": 300, "users": 250, "new_users": 100,
         "bounce_rate": 10.0, "page_depth": 4.0,
         "cart_reaches": 15, "checkout_reaches": 8, "orders": 3, "revenue": 15000.0},
    ]
    rows = build_rows(metrika, "2026-04-20")
    assert len(rows) == 1
    r = rows[0]
    assert _col(r, "campaign_id") == "709091521"
    assert _col(r, "visits") == 400
    assert _col(r, "cart") == 20
    assert _col(r, "orders") == 4
    assert _col(r, "revenue") == 20000.0
    # bounce_w = 30*100 + 10*300 = 6000 → ставка 6000/400 = 15%
    assert _col(r, "bounce_w") == 6000.0
    # depth_w = 2*100 + 4*300 = 1400 → 1400/400 = 3.5
    assert _col(r, "depth_w") == 1400.0


def test_separate_campaigns_stay_separate():
    metrika = [
        {"traffic_source": "ad", "source_engine": "Yandex.Direct",
         "utm_campaign": "111", "direct_campaign_name": "A",
         "visits": 10, "bounce_rate": 0, "page_depth": 1, "cart_reaches": 0,
         "checkout_reaches": 0, "orders": 0, "revenue": 0},
        {"traffic_source": "ad", "source_engine": "Yandex.Direct",
         "utm_campaign": "222", "direct_campaign_name": "B",
         "visits": 20, "bounce_rate": 0, "page_depth": 1, "cart_reaches": 0,
         "checkout_reaches": 0, "orders": 0, "revenue": 0},
    ]
    rows = build_rows(metrika, "2026-04-20")
    assert len(rows) == 2
    assert {_col(r, "campaign_id") for r in rows} == {"111", "222"}


def test_non_ad_channel_empty_campaign_id():
    # SEO/organic — без campaign_id, схлопывается до уровня канала.
    metrika = [
        {"traffic_source": "organic", "source_engine": "Yandex",
         "utm_campaign": "", "direct_campaign_name": "",
         "visits": 50, "bounce_rate": 20, "page_depth": 3, "cart_reaches": 1,
         "checkout_reaches": 0, "orders": 0, "revenue": 0},
    ]
    rows = build_rows(metrika, "2026-04-20")
    assert len(rows) == 1
    assert _col(rows[0], "campaign_id") == ""
    assert _col(rows[0], "channel") == "SEO"
    assert _col(rows[0], "visits") == 50


def test_empty_input():
    assert build_rows([], "2026-04-20") == []
