# -*- coding: utf-8 -*-
"""Недобор трафика: сколько показов кампания не покупает на своей ставке.

Замер боевого прогона (probe_traffic_headroom, Actions run 32855656868,
docs/AGENT-DATA-SOURCES.md) правит две вещи по сравнению с ожиданиями плана:

  * покрытие витрины объёмом трафика — 1.0 ВЕЗДЕ, в каждом разрезе, так что
    порог покрытия ни на чём боевом сегодня не срабатывает и остаётся
    страховкой на случай, если поле начнёт приходить пустым;
  * у кампаний «только сети» объём вырожден: ровно 100.0 у всех трёх. Это не
    «выкупили весь трафик», а «величина не измеряется, отдаётся константа».

Отсюда правило вердикта: судить о недоборе можно только по кампании, которая
показывается ТОЛЬКО на поиске. Сетям и кампаниям без настроек — «неопределённо»,
а не ноль недобора.
"""

from sync.agent.headroom import (NETWORK_ONLY, SEARCH_AND_NETWORK, SEARCH_ONLY,
                                 UNKNOWN_PLACEMENT, computed_rows,
                                 placement_mode, placement_modes,
                                 traffic_headroom)

WINDOW = ("2026-08-01", "2026-08-28")
SEARCH = {"111": SEARCH_ONLY}


def _fact(day, campaign_id, impressions, volume, cost=1000.0):
    return {"fact_date": day, "campaign_id": campaign_id, "cost": cost,
            "impressions": impressions, "avg_traffic_vol": volume}


def test_volume_is_weighted_by_impressions():
    # День с 9000 показов на объёме 50 весит вдевятеро против дня с 1000 на 100.
    rows = [_fact("2026-08-02", "111", 9000, 50.0),
            _fact("2026-08-03", "111", 1000, 100.0)]
    out = traffic_headroom(rows, *WINDOW, SEARCH)
    assert out["111"]["traffic_volume"] == 55.0
    assert out["111"]["headroom_share"] == 0.45


def test_low_volume_with_enough_impressions_has_room():
    rows = [_fact("2026-08-02", "111", 20000, 45.0)]
    assert traffic_headroom(rows, *WINDOW, SEARCH)["111"]["verdict"] == "есть куда расти"


def test_high_volume_is_bought_out():
    rows = [_fact("2026-08-02", "111", 20000, 95.0)]
    assert traffic_headroom(rows, *WINDOW, SEARCH)["111"]["verdict"] == "выкуплен"


def test_small_campaign_is_undetermined_not_optimistic():
    # 300 показов — объём, на котором среднее ничего не значит. Вердикт
    # «есть куда расти» здесь стал бы поводом долить деньги в шум.
    rows = [_fact("2026-08-02", "111", 300, 20.0)]
    assert traffic_headroom(rows, *WINDOW, SEARCH)["111"]["verdict"] == "неопределённо"


def test_days_outside_window_are_ignored():
    rows = [_fact("2026-07-01", "111", 50000, 10.0),
            _fact("2026-08-02", "111", 20000, 90.0)]
    out = traffic_headroom(rows, *WINDOW, SEARCH)
    assert out["111"]["impressions"] == 20000
    assert out["111"]["traffic_volume"] == 90.0


def test_zero_impressions_campaign_is_absent():
    # Кампания без показов не получает вердикта: делить не на что, а строка
    # с нулевым объёмом читалась бы как «весь трафик недобран».
    assert traffic_headroom([_fact("2026-08-02", "111", 0, 0.0)], *WINDOW, SEARCH) == {}


def test_zero_volume_with_live_impressions_is_no_data_not_full_headroom():
    # Прочитать ноль как «недобор 100 %» значит объявить кампанию
    # недоливаемой по несуществующему признаку. По боевому замеру такого
    # сегодня в витрине нет (покрытие 1.0 везде), порог держится страховкой.
    rows = [_fact("2026-08-02", "111", 40_000, 0.0)]
    out = traffic_headroom(rows, *WINDOW, SEARCH)
    assert out["111"]["verdict"] == "неопределённо"
    assert out["111"]["headroom_share"] is None
    assert out["111"]["traffic_volume"] is None


def test_partial_coverage_of_volume_is_undetermined():
    # Покрытие = доля показов, пришедшихся на дни с ненулевым объёмом.
    # Половина показов из дней без объёма — среднее считается по другому
    # набору дней, чем показы, и сравнивать его с порогом нельзя.
    rows = [_fact("2026-08-02", "111", 20_000, 0.0),
            _fact("2026-08-03", "111", 20_000, 40.0)]
    out = traffic_headroom(rows, *WINDOW, SEARCH)
    assert out["111"]["verdict"] == "неопределённо"
    assert out["111"]["volume_coverage"] == 0.5


def test_network_only_campaign_is_undetermined_at_full_volume():
    # Боевой замер: у всех трёх сетевых кампаний объём ровно 100.0 при
    # покрытии 1.0. Это константа, а не выкупленный трафик; headroom_share = 0
    # означал бы «сетям расти некуда», хотя про них не известно ничего.
    rows = [_fact("2026-08-02", "111", 40_000, 100.0)]
    out = traffic_headroom(rows, *WINDOW, {"111": NETWORK_ONLY})
    assert out["111"]["verdict"] == "неопределённо"
    assert out["111"]["headroom_share"] is None
    assert out["111"]["traffic_volume"] is None
    assert out["111"]["placement"] == NETWORK_ONLY


def test_campaign_without_settings_is_undetermined():
    # 15 кампаний кабинета (22 % показов) в edu_campaign_settings отсутствуют —
    # то же пересечение со слепой зоной расхода. Где кампания показывается,
    # неизвестно, а значит неизвестно и что означает её объём трафика.
    rows = [_fact("2026-08-02", "111", 40_000, 84.0)]
    out = traffic_headroom(rows, *WINDOW, {})
    assert out["111"]["verdict"] == "неопределённо"
    assert out["111"]["headroom_share"] is None
    assert out["111"]["placement"] == UNKNOWN_PLACEMENT


def test_computed_rows_carry_support():
    section = traffic_headroom([_fact("2026-08-02", "111", 20000, 45.0)],
                               *WINDOW, SEARCH)
    rows = computed_rows(section)["111"]
    by_key = {r["setting_key"]: r for r in rows}
    assert by_key["traffic_volume"]["value"] == 45.0
    assert by_key["headroom_share"]["value"] == 0.55
    assert by_key["traffic_volume"]["support_n"] == 20000
    assert all(r["setting_kind"] == "headroom" for r in rows)


def test_computed_rows_skip_campaigns_without_a_measurement():
    # Записать «объём 100, недобор 0» по сетевой кампании значит положить в
    # витрину настроек число, которого никто не мерил.
    section = traffic_headroom([_fact("2026-08-02", "111", 40_000, 100.0)],
                               *WINDOW, {"111": NETWORK_ONLY})
    assert computed_rows(section) == {}


def test_placement_mode_reads_both_channels():
    # Источник тот же, что в probe_traffic_headroom.py:85: типы стратегий
    # обоих каналов, SERVING_OFF = канал выключен.
    assert placement_mode("TEXT_CAMPAIGN_HIGHEST_POSITION", "SERVING_OFF") == SEARCH_ONLY
    assert placement_mode("SERVING_OFF", "NETWORK_DEFAULT") == NETWORK_ONLY
    assert placement_mode("AVERAGE_CPA", "NETWORK_DEFAULT") == SEARCH_AND_NETWORK
    assert placement_mode(None, None) == UNKNOWN_PLACEMENT
    assert placement_mode("SERVING_OFF", "SERVING_OFF") == UNKNOWN_PLACEMENT


def test_placement_modes_reads_the_settings_vitrine():
    settings = {
        "111": {"strategy": {"search": {"biddingStrategyType": "AVERAGE_CPA"},
                             "network": {"biddingStrategyType": "SERVING_OFF"}}},
        "222": {"strategy": {"search": None,
                             "network": {"biddingStrategyType": "NETWORK_DEFAULT"}}},
        "333": {},
    }
    assert placement_modes(settings) == {"111": SEARCH_ONLY, "222": NETWORK_ONLY,
                                         "333": UNKNOWN_PLACEMENT}
