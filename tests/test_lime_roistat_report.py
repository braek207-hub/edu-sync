# -*- coding: utf-8 -*-
"""Агрегация визитов/продаж Роистата в платный/бесплатный для отчёта LIME."""
import json
import os

from sync.lime_roistat_report import aggregate_day
from sync.roistat_api import parse_analytics

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "roistat_analytics_day.json")


def load_rows():
    with open(FIXTURE, "r", encoding="utf-8") as f:
        return parse_analytics(json.load(f))


def test_total_is_paid_plus_free():
    """Инвариант таблицы: органический + платный = общий (1576+5652=7228 на скрине)."""
    agg = aggregate_day(load_rows())
    assert agg["total_visits"] == agg["paid_visits"] + agg["free_visits"]
    assert agg["total_orders"] == agg["paid_orders"] + agg["free_orders"]


def test_direct_visits_go_to_free():
    """«Прямые визиты» (2666) — бесплатный трафик, не платный."""
    agg = aggregate_day(load_rows())
    assert agg["free_visits"] >= 2666
    assert agg["paid_visits"] > 0  # Google/Meta/Директ в фикстуре есть


def test_paid_bucket_sums_paid_channels_only():
    """Только каналы с traffic_type=Платный идут в paid_visits."""
    rows = [
        {"channel": "Google Ads 1", "level2": "Поиск", "level2_id": "g",
         "visits": 100, "paid_leads": 5, "leads": 8},
        {"channel": "Прямые визиты", "level2": "", "level2_id": "",
         "visits": 40, "paid_leads": 2, "leads": 3},
        {"channel": "SEO", "level2": "Google", "level2_id": "google",
         "visits": 30, "paid_leads": 1, "leads": 4},
    ]
    agg = aggregate_day(rows)
    assert agg["paid_visits"] == 100
    assert agg["free_visits"] == 70          # Direct 40 + SEO 30
    assert agg["total_visits"] == 170
    assert agg["paid_orders"] == 5
    assert agg["free_orders"] == 3
    assert agg["total_leads"] == 15


def test_empty_rows_give_zeros():
    agg = aggregate_day([])
    assert agg["total_visits"] == 0
    assert agg["total_orders"] == 0
