import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.wordstat import BRAND_PHRASES, _monday, _sunday, aggregate_weekly


def test_brand_phrases_are_the_five():
    assert BRAND_PHRASES == ["lime", "лайм интернет", "лайм купить", "лайм магазин", "лайм одежда"]


def test_monday_normalizes_to_iso_monday():
    assert _monday("2025-06-11") == "2025-06-09"  # среда → понедельник
    assert _monday("2025-06-09") == "2025-06-09"  # уже понедельник
    assert _monday("2026-01-01") == "2025-12-29"  # стык годов


def test_sunday_is_end_of_week():
    assert _sunday("2025-06-11") == "2025-06-15"  # среда → воскресенье
    assert _sunday("2025-06-15") == "2025-06-15"  # уже воскресенье
    assert _sunday("2025-06-09") == "2025-06-15"  # понедельник → воскресенье


def test_aggregate_weekly_sums_phrases_and_parses_string_count():
    # GetDynamics: results[].{date, count(строка int64), share}; даты внутри недели
    responses = [
        {"results": [{"date": "2025-01-06", "count": "100"}, {"date": "2025-01-13", "count": "200"}]},
        {"results": [{"date": "2025-01-08", "count": "10"}, {"date": "2025-01-13", "count": "20"}]},
    ]
    out = aggregate_weekly(responses)
    # 2025-01-06 и 2025-01-08 → одна неделя (Пн 2025-01-06); 2025-01-13 → Пн 2025-01-13
    assert out == {"2025-01-06": 110, "2025-01-13": 220}


def test_aggregate_weekly_empty():
    assert aggregate_weekly([{"results": []}]) == {}