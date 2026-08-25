# -*- coding: utf-8 -*-
"""Разбор ответа Reports API и разрезы витрины в probe недобора трафика.

Поле, которого у API нет, отвечает 400 с BadParams и текстом про FieldNames.
Отличить это от временной ошибки обязательно: «поля нет» — вывод навсегда,
«сервис ответил 502» — повод повторить.

Второй блок тестов — про разрез заполненности объёма трафика по типу кампании.
Объём трафика определён для ПОИСКА; если в сетях/смартах/МК он приходит нулём,
то ноль в витрине означает «не измерялось», а не «недобор 100 %». Считать это
надо до того, как на нулях начнут строиться решения о доливке бюджета.
"""

from probe_traffic_headroom import (
    UNKNOWN_TYPE, bucket_stats, field_verdict, group_stats, placement_mode,
)


def test_ok_when_report_returned():
    assert field_verdict(200, "Date\tClicks\n2026-08-01\t10\n") == "OK"


def test_offline_report_is_ok_too():
    # 201/202 — отчёт принят в очередь: поле принято, данные приедут позже.
    assert field_verdict(201, "") == "OK"


def test_unknown_field_detected_by_message():
    body = '{"error":{"error_code":8000,"error_detail":"Недопустимое значение параметра FieldNames"}}'
    assert field_verdict(400, body) == "FIELD_UNKNOWN"


def test_other_error_is_not_field_verdict():
    body = '{"error":{"error_code":152,"error_detail":"Не хватает средств"}}'
    assert field_verdict(400, body) == "ERROR:152"


def test_non_json_body_keeps_http_code():
    # 502 от балансировщика приходит html-страницей: разобрать её как ошибку
    # API нельзя, но и вердикт «поля нет» из неё не следует.
    assert field_verdict(502, "<html>Bad Gateway</html>") == "ERROR:http502"


def _campaign(campaign_id, impressions, impressions_with_volume,
              traffic_weighted, campaign_type="TEXT_CAMPAIGN",
              search_type="HIGHEST_POSITION", network_type="SERVING_OFF",
              days=7, days_with_volume=7, days_with_win=0, win_weighted=0.0,
              project="vuz"):
    return {"campaign_id": campaign_id, "project": project,
            "campaign_type": campaign_type, "search_type": search_type,
            "network_type": network_type, "days": days,
            "days_with_volume": days_with_volume, "days_with_win": days_with_win,
            "impressions": impressions,
            "impressions_with_volume": impressions_with_volume,
            "traffic_weighted": traffic_weighted, "win_weighted": win_weighted}


def test_placement_mode_reads_strategy_of_both_channels():
    assert placement_mode("HIGHEST_POSITION", "SERVING_OFF") == "только поиск"
    assert placement_mode("SERVING_OFF", "NETWORK_DEFAULT") == "только сети"
    assert placement_mode("HIGHEST_POSITION", "NETWORK_DEFAULT") == "поиск+сети"


def test_placement_mode_without_settings_row_is_unknown():
    # Кампании нет в edu_campaign_settings (архивная, чужой кабинет). Записать
    # её в «поиск» значило бы выдать незнание за знание.
    assert placement_mode(None, None) == UNKNOWN_TYPE


def test_bucket_stats_weights_volume_by_impressions():
    # w_avg_traffic_vol в витрине — значение × показы: среднее берётся делением
    # суммы на показы, а не усреднением по кампаниям.
    stats = bucket_stats([_campaign("1", 9_000, 9_000, 50.0 * 9_000),
                          _campaign("2", 1_000, 1_000, 100.0 * 1_000)])
    assert stats["campaigns"] == 2
    assert stats["impressions"] == 10_000
    assert stats["avg_traffic_volume"] == 55.0
    assert stats["volume_coverage"] == 1.0


def test_bucket_stats_counts_impressions_without_volume():
    # Половина показов пришлась на дни с нулевым объёмом: покрытие 0.5, и
    # среднее посчитано по другому набору дней, чем показы.
    stats = bucket_stats([_campaign("1", 20_000, 10_000, 40.0 * 10_000,
                                    days=10, days_with_volume=5)])
    assert stats["impressions_with_volume"] == 10_000
    assert stats["volume_coverage"] == 0.5
    assert stats["days_with_volume"] == 5


def test_bucket_stats_of_empty_bucket_has_no_average():
    stats = bucket_stats([])
    assert stats["campaigns"] == 0
    assert stats["impressions"] == 0
    assert stats["avg_traffic_volume"] is None
    assert stats["volume_coverage"] is None


def test_group_stats_splits_search_from_networks():
    rows = [_campaign("1", 20_000, 20_000, 60.0 * 20_000),
            _campaign("2", 30_000, 0, 0.0, campaign_type="SMART_CAMPAIGN",
                      search_type=None, network_type=None, days_with_volume=0)]
    out = group_stats(rows, lambda r: r["campaign_type"] or UNKNOWN_TYPE)
    assert out["TEXT_CAMPAIGN"]["avg_traffic_volume"] == 60.0
    assert out["SMART_CAMPAIGN"]["impressions"] == 30_000
    assert out["SMART_CAMPAIGN"]["impressions_with_volume"] == 0
    assert out["SMART_CAMPAIGN"]["volume_coverage"] == 0.0
    # Среднее выходит нулём, и само по себе оно неотличимо от «объём трафика
    # действительно ноль». Отличает покрытие: 0 показов с объёмом = поля не было.
    assert out["SMART_CAMPAIGN"]["avg_traffic_volume"] == 0.0
