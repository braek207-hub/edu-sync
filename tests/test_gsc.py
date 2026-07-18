import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.gsc import REGIONS, aggregate_by_country, parse_search_analytics
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


def test_regions_cover_kz_and_gcc():
    assert REGIONS["kz"]["sites"] == ["https://limestore.com/"]
    assert REGIONS["kz"]["countries"] == {"kaz": ""}
    gcc = REGIONS["gcc"]
    # Эксперимент 2026-07-18: корневой домен временно выключен, остались 6 витрин Залива.
    # Вернётся — поправить на 7 и раскомментировать проверку ниже.
    assert len(gcc["sites"]) == 6
    assert "https://limestore.com/" not in gcc["sites"]
    assert all(s.endswith(".limestore.com/") for s in gcc["sites"])
    assert gcc["countries"]["are"] == "ОАЭ"
    assert gcc["countries"]["sau"] == "Саудовская Аравия"
    assert len(gcc["countries"]) == 6


def test_aggregate_by_country_sums_across_resources():
    # один запрос в одну неделю с ДВУХ ресурсов → показы и клики складываются,
    # а не перезаписываются (регрессия на старый дедуп по (date, query))
    rows = [
        {"query": "lime", "date": "2025-06-02", "clicks": 10, "impressions": 100,
         "country": "ОАЭ", "site": "https://ae.limestore.com/"},
        {"query": "lime", "date": "2025-06-02", "clicks": 4, "impressions": 40,
         "country": "ОАЭ", "site": "https://limestore.com/"},
    ]
    assert aggregate_by_country(rows, "gcc") == {
        ("2025-06-02", "ОАЭ"): {"clicks": 14, "impressions": 140},
    }


def test_aggregate_by_country_keeps_countries_apart():
    rows = [
        {"query": "lime", "date": "2025-06-02", "clicks": 10, "impressions": 100, "country": "ОАЭ"},
        {"query": "lime", "date": "2025-06-03", "clicks": 5, "impressions": 50, "country": "Катар"},
    ]
    out = aggregate_by_country(rows, "gcc")
    assert out[("2025-06-02", "ОАЭ")] == {"clicks": 10, "impressions": 100}
    assert out[("2025-06-02", "Катар")] == {"clicks": 5, "impressions": 50}  # та же ISO-неделя


def test_aggregate_by_country_keeps_arabic_and_drops_non_brand():
    rows = [
        {"query": "محل لايم", "date": "2025-06-02", "clicks": 7, "impressions": 70, "country": "ОАЭ"},
        {"query": "linen pants", "date": "2025-06-02", "clicks": 99, "impressions": 999, "country": "ОАЭ"},
    ]
    assert aggregate_by_country(rows, "gcc") == {
        ("2025-06-02", "ОАЭ"): {"clicks": 7, "impressions": 70},
    }


def test_gsc_rows_aggregate_brand_only_by_week():
    # исторический ряд RU/KZ: та же бренд-логика через aggregate_seo_weekly Вебмастера
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
