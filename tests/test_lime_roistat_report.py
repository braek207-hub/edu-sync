# -*- coding: utf-8 -*-
"""Агрегация визитов/продаж Роистата в платный/бесплатный для отчёта LIME."""
import json
import os

import sync.lime_roistat_report as rep
from sync.lime_roistat_report import _fetch_agg_guarded, _orders_row, _traffic_row, aggregate_day
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


def test_row_builders_match_headers():
    """Порядок значений в строке = порядок колонок заголовка (иначе съедет запись)."""
    agg = {"free_visits": 70, "paid_visits": 100, "total_visits": 170,
           "paid_orders": 5, "free_orders": 3, "total_orders": 8}
    tr = _traffic_row("2026-07-04", agg)
    assert tr == ["Сб 04.07.2026", 70, 100, 170, 170, 2026, 7]
    orow = _orders_row("2026-07-04", agg)
    assert orow == ["Сб 04.07.2026", 5, 3, 8, 2026, 7]


def _patch_fetch(monkeypatch, side):
    monkeypatch.setattr(rep, "EMPTY_RETRY_SLEEP", 0)
    monkeypatch.setattr(rep, "EMPTY_RETRIES", 3)
    calls = {"n": 0}

    def fake(day, project, key):
        calls["n"] += 1
        return side(calls["n"])

    monkeypatch.setattr(rep, "fetch_day", fake)
    return calls


def test_guarded_returns_data_when_present(monkeypatch):
    rows = [{"channel": "Google Ads 1", "level2": "Поиск", "level2_id": "g",
             "visits": 100, "paid_leads": 5, "leads": 8}]
    _patch_fetch(monkeypatch, lambda n: rows)
    agg, has = _fetch_agg_guarded("2026-07-04", "k")
    assert has is True
    assert agg["paid_visits"] == 100


def test_guarded_empty_day_is_not_written(monkeypatch):
    """Пустой день (данные не готовы) → has_data=False, чтобы не затереть прежнее нулями."""
    calls = _patch_fetch(monkeypatch, lambda n: [])
    agg, has = _fetch_agg_guarded("2026-07-04", "k")
    assert has is False
    assert agg["total_visits"] == 0
    assert calls["n"] == 3  # исчерпал ретраи пустого ответа


def test_guarded_survives_roistat_error(monkeypatch):
    """Ошибка Роистата после его ретраев не роняет прогон — день просто пропускается."""
    def boom(n):
        raise RuntimeError("не забран после 3 попыток")
    _patch_fetch(monkeypatch, boom)
    agg, has = _fetch_agg_guarded("2026-07-04", "k")
    assert has is False


def test_guarded_recovers_on_late_data(monkeypatch):
    """Первые попытки пусто, затем данные пришли → пишем их."""
    rows = [{"channel": "Прямые визиты", "level2": "", "level2_id": "",
             "visits": 40, "paid_leads": 2, "leads": 3}]
    _patch_fetch(monkeypatch, lambda n: rows if n >= 2 else [])
    agg, has = _fetch_agg_guarded("2026-07-04", "k")
    assert has is True
    assert agg["free_visits"] == 40
