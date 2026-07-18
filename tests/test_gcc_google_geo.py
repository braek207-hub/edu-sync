import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.gcc_google_geo import aggregate_geo_spend

# Курс-заглушка: AED→₽ 20, USD→₽ 80.
RATES = {"AED": 20.0, "USD": 80.0}


def rate_for(currency):
    return RATES[currency]


def test_geo_spend_by_country_converted_to_rub():
    db_rows = [
        {"country": "ОАЭ", "cost": 50.0, "currency": "AED"},
        {"country": "Катар", "cost": 12.0, "currency": "AED"},
    ]
    rows = aggregate_geo_spend(db_rows, "2026-07-17", rate_for)
    by_country = {r["country"]: r for r in rows}
    assert by_country["ОАЭ"]["cost"] == 1000.0
    assert by_country["Катар"]["cost"] == 240.0
    assert all(r["channel"] == "SEM" and r["subchannel"] == "Google.Adwords" for r in rows)
    assert all(r["traffic_type"] == "Платный" and r["date"] == "2026-07-17" for r in rows)


def test_geo_spend_sums_campaigns_within_country():
    db_rows = [
        {"country": "ОАЭ", "cost": 10.0, "currency": "AED"},
        {"country": "ОАЭ", "cost": 5.0, "currency": "AED"},
    ]
    rows = aggregate_geo_spend(db_rows, "2026-07-17", rate_for)
    assert len(rows) == 1 and rows[0]["cost"] == 300.0


def test_geo_spend_residual_row_keeps_empty_country_as_none():
    """Строка-остаток скрипта (country='') → country=None: тот же смысл, что «вне разбивки»."""
    db_rows = [{"country": "", "cost": 3.0, "currency": "AED"}]
    rows = aggregate_geo_spend(db_rows, "2026-07-17", rate_for)
    assert len(rows) == 1 and rows[0]["country"] is None and rows[0]["cost"] == 60.0


def test_geo_spend_skips_zero_cost():
    db_rows = [
        {"country": "Оман", "cost": 0.0, "currency": "AED"},
        {"country": "ОАЭ", "cost": 1.0, "currency": "AED"},
    ]
    rows = aggregate_geo_spend(db_rows, "2026-07-17", rate_for)
    assert [r["country"] for r in rows] == ["ОАЭ"]


def test_geo_spend_unknown_currency_skipped_not_guessed():
    """Курса нет — строку пропускаем, а не считаем 1:1 (иначе тихо соврём в рублях)."""
    db_rows = [
        {"country": "ОАЭ", "cost": 10.0, "currency": "XYZ"},
        {"country": "Катар", "cost": 2.0, "currency": "AED"},
    ]

    def rate_or_raise(currency):
        if currency not in RATES:
            raise KeyError(currency)
        return RATES[currency]

    rows = aggregate_geo_spend(db_rows, "2026-07-17", rate_or_raise)
    assert [r["country"] for r in rows] == ["Катар"]


def test_geo_spend_empty_input():
    assert aggregate_geo_spend([], "2026-07-17", rate_for) == []
