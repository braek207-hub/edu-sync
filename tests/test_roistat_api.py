# -*- coding: utf-8 -*-
"""Клиент Roistat API: разбор ответа и три ловушки контракта."""
import json
import os

from sync.roistat_api import METRICS, day_period, parse_analytics

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "roistat_analytics_day.json")


def load():
    with open(FIXTURE, "r", encoding="utf-8") as f:
        return json.load(f)


def by_channel(rows, name):
    return [r for r in rows if r["channel"] == name]


def test_day_period_is_exclusive_on_to():
    """`to` у Роистата эксклюзивен: from==to возвращает нули, день N = [N, N+1)."""
    assert day_period("2026-06-18") == {"from": "18.06.2026", "to": "19.06.2026"}


def test_day_period_crosses_month_boundary():
    assert day_period("2026-06-30") == {"from": "30.06.2026", "to": "01.07.2026"}


def test_canceled_revenue_metric_name_is_cyrillic():
    """Ловушка API: в revenue_сanceled буква «с» кириллическая (U+0441).

    Латиницей метрика не существует, API отвечает request_data_validation_error.
    """
    name = [m for m in METRICS if m.startswith("revenue_")][0]
    assert "с" in name
    assert name != "revenue_canceled"


def test_channel_names_are_denbspd():
    """Роистат отдаёт 'Google\\xa0Ads\\xa01'. Без нормализации весь платный трафик
    молча уехал бы в «Others» — 128 тыс. визитов в месяц."""
    rows = parse_analytics(load())
    names = {r["channel"] for r in rows}
    assert "Google Ads 1" in names
    assert not any("\xa0" in n for n in names)


def test_channel_read_from_title_when_value_empty():
    """У «Прямых визитов» marker_level_1.value пустой, читаемое имя всегда в title."""
    rows = parse_analytics(load())
    direct = by_channel(rows, "Прямые визиты")
    assert len(direct) == 1
    assert direct[0]["visits"] == 2666
    assert direct[0]["leads"] == 319
    assert direct[0]["paid_leads"] == 293


def test_level_values_carry_real_campaign_ids():
    """value уровня — настоящий campaign_id, совпадающий с нашими кабинетами."""
    rows = parse_analytics(load())
    google = [r for r in by_channel(rows, "Google Ads 1") if r["level2"] == "Поиск"][0]
    assert google["level3_id"] == "23237404958"
    assert google["level3"].startswith("Бренд. Поиск 2")
    assert google["level2_id"] == "g"  # код типа, не id


def test_facebook_campaign_id_on_level2():
    """У Meta кампания на level_2, адсет на level_3 — оба с числовыми id."""
    rows = parse_analytics(load())
    fb = by_channel(rows, "Facebook")[0]
    assert fb["level2_id"].isdigit()
    assert fb["level3_id"].isdigit()
    assert fb["level2_id"] != fb["level3_id"]


def test_cost_is_read_including_fraction():
    rows = parse_analytics(load())
    fb = by_channel(rows, "Facebook")[0]
    assert fb["cost"] > 0
    assert fb["cost"] != int(fb["cost"])  # расход приходит дробным


def test_missing_metric_is_zero_not_crash():
    resp = load()
    resp["data"][0]["items"][0]["metrics"] = []
    rows = parse_analytics(resp)
    assert rows[0]["visits"] == 0.0
    assert rows[0]["cost"] == 0.0


def test_empty_response_gives_no_rows():
    assert parse_analytics({"data": []}) == []
    assert parse_analytics({}) == []


from sync.roistat_api import COHORT_METRICS, cohort_period, parse_cohort


def test_cohort_period_is_exclusive_on_to():
    assert cohort_period("2026-05-01", "2026-07-20") == {"from": "01.05.2026", "to": "20.07.2026"}


def test_cohort_metrics_include_new_and_repeat():
    """Новизна когорты берётся в той же выгрузке: new_sales+repeatedSales=paidLeadCount по визиту."""
    assert "new_sales" in COHORT_METRICS
    assert "repeatedSales" in COHORT_METRICS
    assert "paidLeadCount" in COHORT_METRICS
    assert "paidLeadsPrice" in COHORT_METRICS


def test_parse_cohort_reads_visit_date_from_daily_bucket():
    resp = {"data": [{"items": [
        {"dimensions": {
            "daily": {"value": "2026-06-08", "title": "2026-06-08"},
            "marker_level_1": {"value": "", "title": "Прямые\xa0визиты"},
            "marker_level_2": {"value": "", "title": ""},
            "marker_level_3": {"value": "", "title": ""}},
         "metrics": [
            {"metric_name": "paidLeadCount", "value": 409},
            {"metric_name": "paidLeadsPrice", "value": 18031810},
            {"metric_name": "new_sales", "value": 86},
            {"metric_name": "repeatedSales", "value": 323}]}]}]}
    rows = parse_cohort(resp)
    assert len(rows) == 1
    r = rows[0]
    assert r["visit_date"] == "2026-06-08"
    assert r["channel"] == "Прямые визиты"          # NBSP нормализован
    assert r["cohort_orders"] == 409
    assert r["cohort_revenue"] == 18031810
    assert r["cohort_new"] == 86
    assert r["cohort_repeat"] == 323
    assert r["cohort_new"] + r["cohort_repeat"] == r["cohort_orders"]


def test_parse_cohort_missing_metric_is_zero():
    resp = {"data": [{"items": [
        {"dimensions": {"daily": {"value": "2026-06-08", "title": "2026-06-08"},
                        "marker_level_1": {"value": "g", "title": "Google\xa0Ads\xa01"},
                        "marker_level_2": {}, "marker_level_3": {}},
         "metrics": []}]}]}
    rows = parse_cohort(resp)
    assert rows[0]["cohort_orders"] == 0.0
    assert rows[0]["channel"] == "Google Ads 1"
