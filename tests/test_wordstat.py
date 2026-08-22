import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.wordstat import BRAND_PHRASES, _monday, _sunday, aggregate_daily, aggregate_weekly


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


def test_aggregate_daily_sums_phrases_and_parses_string_count():
    # PERIOD_DAILY: точка = день, суммируем фразы по одинаковой дате
    responses = [
        {"results": [{"date": "2026-08-01", "count": "100"}, {"date": "2026-08-02", "count": "200"}]},
        {"results": [{"date": "2026-08-01", "count": "10"}]},
    ]
    assert aggregate_daily(responses) == {"2026-08-01": 110, "2026-08-02": 200}


def test_aggregate_daily_truncates_rfc3339_to_day():
    # date может прийти RFC3339-таймстампом — ключ дня всё равно YYYY-MM-DD
    responses = [{"results": [{"date": "2026-08-01T00:00:00Z", "count": "5"}]}]
    assert aggregate_daily(responses) == {"2026-08-01": 5}


def test_aggregate_daily_empty():
    assert aggregate_daily([{"results": []}]) == {}

def test_fetch_omits_region_filter_by_default(monkeypatch):
    """Ручной канон Павла собирается БЕЗ фильтра региона: сверка 2026-08-20 по неделе
    10.08 совпала бит-в-бит (Σ=183767), «Россия=225» давала стабильно −3%.
    Дефолт обеих выборок — без поля regions; явный список продолжает передаваться."""
    import sync.wordstat as ws

    captured = []
    monkeypatch.setattr(ws, "_post_dynamics", lambda body: captured.append(body) or {})

    ws.fetch_phrase("lime", "2026-08-10", "2026-08-16")
    ws.fetch_phrase_daily("lime", "2026-08-10", "2026-08-16")
    assert all("regions" not in b for b in captured)

    ws.fetch_phrase("lime", "2026-08-10", "2026-08-16", regions=["225"])
    assert captured[-1]["regions"] == ["225"]
