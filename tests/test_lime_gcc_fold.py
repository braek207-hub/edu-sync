# -*- coding: utf-8 -*-
"""Свёртка строк GCC по зерну витрины — заказы приложения и его трафик едут одной строкой.

Замер на проде 29.08.2026: 14 пар строк из 384 360 лежали на одном зерне
(date, data_source, region, country, channel, subchannel, traffic_type, campaign_id,
campaign_name) — в одной заказы без визитов, в другой визиты без заказов. Это
единственное место во всей таблице, где зерно не уникально.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.lime_gcc import COLUMNS, fold_by_grain

I = {name: i for i, name in enumerate(COLUMNS)}


def row(**kw):
    """Кортеж lime_stats в порядке COLUMNS: зерно задаётся явно, метрики — нулями."""
    base = {
        "date": "2026-08-26", "data_source": "app", "region": "gcc", "country": "ОАЭ",
        "channel": "Direct", "subchannel": "Direct", "traffic_type": "Бесплатный",
        "campaign_id": None, "campaign_name": "",
        "cost": 0.0, "clicks": 0, "impressions": 0, "sessions": 0, "users": 0, "clients": 0,
        "purchases_count": 0, "purchases_revenue": 0.0, "customers": 0,
        "new_users": 0, "new_customers": 0, "new_customers_revenue": 0.0,
        "bounce_rate": None, "page_depth": None, "cart_reaches": 0, "checkout_reaches": 0,
    }
    base.update(kw)
    return tuple(base[name] for name in COLUMNS)


def test_orders_and_traffic_of_one_grain_become_one_row():
    # Ровно тот случай с прода: id 1361184 (заказы) и 1361197 (трафик).
    rows = fold_by_grain([
        row(purchases_count=1, purchases_revenue=59268.17),
        row(users=226, sessions=525),
    ])
    assert len(rows) == 1
    r = rows[0]
    assert r[I["purchases_count"]] == 1
    assert r[I["purchases_revenue"]] == 59268.17
    assert r[I["users"]] == 226
    assert r[I["sessions"]] == 525


def test_different_grain_stays_apart():
    rows = fold_by_grain([
        row(country="ОАЭ", users=10),
        row(country="Кувейт", users=7),
        row(channel="SEO", subchannel="SEO Yandex", users=3),
        row(campaign_id="123", users=5),
    ])
    assert len(rows) == 4


def test_grain_key_is_exactly_the_dashboard_group_by():
    # Зерно — первые девять колонок; десятая (cost) уже метрика и в ключ не входит.
    assert COLUMNS[:9] == (
        "date", "data_source", "region", "country", "channel", "subchannel",
        "traffic_type", "campaign_id", "campaign_name",
    )
    rows = fold_by_grain([row(cost=10.0), row(cost=5.0)])
    assert len(rows) == 1
    assert rows[0][I["cost"]] == 15.0


def test_averages_are_reweighted_by_sessions_not_summed():
    rows = fold_by_grain([
        row(sessions=100, bounce_rate=0.4, page_depth=2.0),
        row(sessions=300, bounce_rate=0.8, page_depth=6.0),
    ])
    assert len(rows) == 1
    # (0.4×100 + 0.8×300) / 400 = 0.7 — не 1.2 и не 0.6.
    assert rows[0][I["bounce_rate"]] == 0.7
    assert rows[0][I["page_depth"]] == 5.0


def test_missing_average_does_not_erase_the_present_one():
    rows = fold_by_grain([
        row(purchases_count=3),                       # заказы: отказов нет вовсе
        row(sessions=50, bounce_rate=0.25),           # трафик: отказы есть
    ])
    assert rows[0][I["bounce_rate"]] == 0.25
    # И в обратном порядке — свёртка не зависит от того, кто пришёл первым.
    rows = fold_by_grain([
        row(sessions=50, bounce_rate=0.25),
        row(purchases_count=3),
    ])
    assert rows[0][I["bounce_rate"]] == 0.25


def test_single_row_passes_through_unchanged():
    one = row(users=5, sessions=9, bounce_rate=0.3)
    assert fold_by_grain([one]) == [one]


def test_empty_input():
    assert fold_by_grain([]) == []
