# -*- coding: utf-8 -*-
"""Парсинг официального AdMetrica stat/data → строки lime_media_stats.
Фиксирует порядок метрик (индексы) и что пост-вью доход берётся из ответа, а не оценивается."""
import json
from sync import lime_mediametrica as mm

# Ответ /stat/data по одной кампании, dimensions=am:e:date, метрики в порядке _metrics():
# [renders, users, clicks, ecommerce<cur>RevenuePostView, goalPurchase, goalCart, goalCheckout, videoComplete]
FAKE_DAILY = [
    {"dimensions": [{"name": "2026-08-17"}], "metrics": [63050, 31883, 0, 250000.0, 12, 180, 40, 0]},
    {"dimensions": [{"name": "2026-08-18"}], "metrics": [50000, 25000, 3, 99999.5, 7, 90, 15, 500]},
]
FAKE_CAMPS = [
    {"campaign_id": 100731, "name": "TV 17.08.26-31.08.26", "advertiser_name": "Lime",
     "date_start": "2026-08-17", "date_end": "2026-08-31"},
]


def test_build_rows_maps_metrics_and_real_revenue(monkeypatch):
    monkeypatch.setattr(mm, "fetch_daily", lambda cid, d1, d2: FAKE_DAILY)
    rows = mm.build_rows(FAKE_CAMPS, "2026-08-01", "2026-08-18")

    assert len(rows) == 2
    r0 = rows[0]
    assert r0["source"] == "mediametrica" and r0["region"] == "ru"
    assert r0["campaign_group"] == "TV 17.08.26-31.08.26"
    assert r0["impressions"] == 63050          # renders
    assert r0["reach"] == 31883                # users = охват
    conv = json.loads(r0["conversions"])
    assert conv["pv_revenue"] == 250000.0      # РЕАЛЬНЫЙ доход из ответа (индекс 3), не оценка
    assert conv["pv_purchase"] == 12
    assert conv["pv_cart"] == 180
    assert conv["pv_checkout"] == 40
    # видео-досмотры со второго дня
    assert rows[1]["video_completes"] == 500
    assert json.loads(rows[1]["conversions"])["pv_revenue"] == 99999.5


def test_flight_outside_window_skipped(monkeypatch):
    monkeypatch.setattr(mm, "fetch_daily", lambda cid, d1, d2: FAKE_DAILY)
    past = [{"campaign_id": 1, "name": "Old", "advertiser_name": "Lime",
             "date_start": "2025-01-01", "date_end": "2025-02-01"}]
    assert mm.build_rows(past, "2026-08-01", "2026-08-18") == []


def test_metrics_string_has_postview_revenue_and_goals():
    m = mm._metrics()
    assert "RevenuePostView" in m
    assert f"goal{mm.PURCHASE_GOAL}Reaches" in m
    assert m.count("am:e:") == 8  # ровно 8 метрик в одном запросе
