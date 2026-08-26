import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.yandex_organic import REGIONS, countries_filter, engines_filter, parse_rows


def test_parse_rows_maps_days_and_countries():
    """Ответ Stat API (dims = date, regionCountryName) → плоские строки витрины."""
    resp = {
        "query": {"dimensions": ["ym:s:date", "ym:s:regionCountryName"]},
        "data": [
            {"dimensions": [{"name": "2026-08-10"}, {"name": "United Arab Emirates"}],
             "metrics": [42.0]},
            {"dimensions": [{"name": "2026-08-10"}, {"name": "Qatar"}], "metrics": [3.0]},
        ],
    }
    assert parse_rows(resp, REGIONS["gcc"]) == [
        {"day": "2026-08-10", "country": "ОАЭ", "visits": 42},
        {"day": "2026-08-10", "country": "Катар", "visits": 3},
    ]


def test_parse_rows_reads_dimension_positions_from_echo():
    """Позиции измерений берутся из эха запроса, а не из константы: перестановка
    измерений в запросе не должна ломать разбор."""
    resp = {
        "query": {"dimensions": ["ym:s:regionCountryName", "ym:s:date"]},
        "data": [
            {"dimensions": [{"name": "Kazakhstan"}, {"name": "2026-08-10"}], "metrics": [200.0]},
        ],
    }
    assert parse_rows(resp, REGIONS["kz"]) == [
        {"day": "2026-08-10", "country": "", "visits": 200},
    ]


def test_parse_rows_skips_foreign_countries():
    """Страна вне карты региона пропускается: фильтр запроса её вернуть не должен,
    но если вернёт — она не попадёт в чужой ряд."""
    resp = {
        "query": {"dimensions": ["ym:s:date", "ym:s:regionCountryName"]},
        "data": [
            {"dimensions": [{"name": "2026-08-10"}, {"name": "Russia"}], "metrics": [9999.0]},
        ],
    }
    assert parse_rows(resp, REGIONS["kz"]) == []


def test_kz_country_is_empty_string_in_showcase():
    """У KZ регион = страна, поэтому строка витрины пишется с пустой страной —
    как в lime_gsc_seo (селектор стран есть только у Залива)."""
    assert REGIONS["kz"] == {"Kazakhstan": ""}


def test_filters_are_or_joined_and_parenthesised():
    """Фильтр Stat API — OR-цепочка в скобках: без скобок AND между блоками
    склеился бы не с тем операндом и ряд молча уехал бы."""
    assert engines_filter() == (
        "(ym:s:lastsignSearchEngine=='yandex_search'"
        " OR ym:s:lastsignSearchEngine=='yandex_mobile')"
    )
    assert countries_filter(["Kazakhstan"]) == "(ym:s:regionCountryName=='Kazakhstan')"
