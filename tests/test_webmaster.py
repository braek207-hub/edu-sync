import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.webmaster import (
    aggregate_seo_weekly,
    is_brand_query,
    parse_query_analytics,
)


def test_is_brand_query_matches_lime_and_cyrillic():
    assert is_brand_query("lime магазин")
    assert is_brand_query("лайм одежда")
    assert is_brand_query("LIME")            # регистронезависимо
    assert not is_brand_query("платье женское")
    assert not is_brand_query("")


def test_parse_query_analytics_extracts_clicks_impressions_by_date():
    # реальная структура ответа query-analytics/list (см. фикстуру probe)
    data = {
        "count": 2,
        "text_indicator_to_statistics": [
            {
                "text_indicator": {"type": "QUERY", "value": "lime"},
                "statistics": [
                    {"date": "2026-06-29", "field": "CTR", "value": 23.8},
                    {"date": "2026-06-29", "field": "IMPRESSIONS", "value": 4537.0},
                    {"date": "2026-06-29", "field": "CLICKS", "value": 1080.0},
                    {"date": "2026-06-30", "field": "CLICKS", "value": 900.0},
                ],
            }
        ],
    }
    out = parse_query_analytics(data)
    assert out["lime"] == [
        {"date": "2026-06-29", "clicks": 1080, "impressions": 4537},
        {"date": "2026-06-30", "clicks": 900, "impressions": 0},
    ]


def test_aggregate_seo_weekly_sums_brand_only_by_week():
    rows = [
        {"query": "lime", "date": "2025-06-02", "clicks": 50, "impressions": 500},   # Пн
        {"query": "лайм купить", "date": "2025-06-03", "clicks": 10, "impressions": 100},  # Вт → та же неделя
        {"query": "туфли", "date": "2025-06-03", "clicks": 99, "impressions": 999},  # не бренд
        {"query": "lime", "date": "2025-06-09", "clicks": 5, "impressions": 50},     # след. неделя
    ]
    out = aggregate_seo_weekly(rows)
    assert out == {
        "2025-06-02": {"clicks": 60, "impressions": 600},
        "2025-06-09": {"clicks": 5, "impressions": 50},
    }