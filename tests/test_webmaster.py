import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.webmaster import (
    aggregate_seo_daily,
    aggregate_seo_weekly,
    is_brand_query,
    parse_query_analytics,
    seo_daily_fresh_target,
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


def test_aggregate_seo_daily_sums_brand_only_by_day():
    # запросы × хосты одного дня суммируются; не-бренд отфильтрован
    rows = [
        {"query": "lime", "date": "2026-08-03", "clicks": 50, "impressions": 500},
        {"query": "лайм купить", "date": "2026-08-03", "clicks": 10, "impressions": 100},  # тот же день
        {"query": "туфли", "date": "2026-08-03", "clicks": 99, "impressions": 999},  # не бренд
        {"query": "lime", "date": "2026-08-04", "clicks": 5, "impressions": 50},
    ]
    assert aggregate_seo_daily(rows) == {
        "2026-08-03": {"clicks": 60, "impressions": 600},
        "2026-08-04": {"clicks": 5, "impressions": 50},
    }


def test_aggregate_seo_daily_no_monday_collapse():
    # дни одной недели остаются отдельными точками (в отличие от aggregate_seo_weekly)
    rows = [
        {"query": "lime", "date": "2026-08-03", "clicks": 1, "impressions": 10},  # Пн
        {"query": "lime", "date": "2026-08-04", "clicks": 2, "impressions": 20},  # Вт
    ]
    out = aggregate_seo_daily(rows)
    assert sorted(out) == ["2026-08-03", "2026-08-04"]
    assert aggregate_seo_weekly(rows) == {"2026-08-03": {"clicks": 3, "impressions": 30}}


def test_aggregate_seo_daily_truncates_timestamp_and_handles_empty():
    # ключ дня стабилен к таймстампу; пусто/не-бренд → {}
    rows = [{"query": "lime", "date": "2026-08-03T00:00:00Z", "clicks": 5, "impressions": 50}]
    assert aggregate_seo_daily(rows) == {"2026-08-03": {"clicks": 5, "impressions": 50}}
    assert aggregate_seo_daily([]) == {}
    assert aggregate_seo_daily(
        [{"query": "платье", "date": "2026-08-03", "clicks": 9, "impressions": 9}]
    ) == {}


def test_seo_daily_fresh_target_is_today_minus_three():
    # лаг Вебмастера ~2 дня → «свежо», когда есть день вчера-2 (today-3)
    assert seo_daily_fresh_target(dt.date(2026, 8, 8)) == "2026-08-05"

def test_drop_leading_partial_weeks_guards_sliding_window():
    """Окно API скользит: неделя без своих первых дней не должна перезаписываться.

    Порча 2026-08: закрытые недели в lime_brand_seo деградировали до суммы одного
    последнего дня (5107 кликов вместо ~40000), потому что upsert писал усечённую
    слева сумму. Неделя пишется, только если её понедельник внутри окна."""
    from sync.webmaster import drop_leading_partial_weeks

    # окно среда 06-25 … среда 07-08: неделя 06-23 усечена, 06-30 полная, 07-07 текущая
    rows = [
        {"query": "lime", "date": "2025-06-25", "clicks": 5, "impressions": 50},
        {"query": "lime", "date": "2025-06-30", "clicks": 7, "impressions": 70},
        {"query": "lime", "date": "2025-07-08", "clicks": 3, "impressions": 30},
    ]
    weekly = aggregate_seo_weekly(rows)
    out = drop_leading_partial_weeks(weekly, rows)
    assert "2025-06-23" not in out          # усечена слева → не трогаем сохранённое
    assert out["2025-06-30"] == {"clicks": 7, "impressions": 70}
    assert out["2025-07-07"] == {"clicks": 3, "impressions": 30}  # правый край растёт — ок


def test_drop_leading_partial_weeks_empty_rows_noop():
    from sync.webmaster import drop_leading_partial_weeks

    assert drop_leading_partial_weeks({}, []) == {}
    assert drop_leading_partial_weeks({"2025-06-23": {"clicks": 1, "impressions": 1}}, []) == {
        "2025-06-23": {"clicks": 1, "impressions": 1}
    }
