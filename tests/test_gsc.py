import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.gsc import parse_search_analytics
from sync.webmaster import aggregate_seo_weekly


def test_parse_search_analytics_maps_keys_to_query_date():
    # реальная структура ответа searchanalytics.query (подтверждена зондом)
    resp = {
        "rows": [
            {"keys": ["2026-06-17", "lime"], "clicks": 16542.0, "impressions": 25031.0,
             "ctr": 0.66, "position": 2.28},
            {"keys": ["2026-06-18", "лайм купить"], "clicks": 100.0, "impressions": 900.0},
            {"keys": ["2026-06-18"], "clicks": 5.0, "impressions": 9.0},  # мало ключей → пропуск
        ]
    }
    out = parse_search_analytics(resp)
    assert out == [
        {"date": "2026-06-17", "query": "lime", "clicks": 16542, "impressions": 25031},
        {"date": "2026-06-18", "query": "лайм купить", "clicks": 100, "impressions": 900},
    ]


def test_parse_search_analytics_empty():
    assert parse_search_analytics({}) == []
    assert parse_search_analytics({"rows": []}) == []


def test_gsc_rows_aggregate_brand_only_by_week():
    # gsc.py переиспользует aggregate_seo_weekly Вебмастера → та же бренд-логика
    rows = [
        {"query": "lime", "date": "2025-06-02", "clicks": 50, "impressions": 500},        # Пн
        {"query": "лайм магазин", "date": "2025-06-03", "clicks": 10, "impressions": 100},  # та же неделя
        {"query": "платье", "date": "2025-06-03", "clicks": 99, "impressions": 999},       # не бренд → выкидывается
        {"query": "lime", "date": "2025-06-09", "clicks": 5, "impressions": 50},           # след. неделя
    ]
    assert aggregate_seo_weekly(rows) == {
        "2025-06-02": {"clicks": 60, "impressions": 600},
        "2025-06-09": {"clicks": 5, "impressions": 50},
    }
