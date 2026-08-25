# -*- coding: utf-8 -*-
"""Э3.1: кривые насыщения — эластичность из двух DiD-источников,
β и предельная цена эффективного лида."""

import math
from datetime import date, timedelta

from sync.agent.saturation import (
    BETA_MIN,
    computed_rows,
    saturation_curves,
    weekly_pair_observations,
)

# Понедельник — недельные агрегаты в модуле календарные (пн–вс).
MONDAY = date(2026, 6, 1) - timedelta(days=date(2026, 6, 1).weekday())


def _days(week_index, count=7):
    start = MONDAY + timedelta(days=7 * week_index)
    return [(start + timedelta(days=i)).isoformat() for i in range(count)]


def _facts(campaign_id, week_index, cost_per_day, leads_per_day,
           direction="spo", days=7):
    return [{
        "fact_date": day, "campaign_id": campaign_id, "direction": direction,
        "cost": cost_per_day, "eff_leads": leads_per_day,
    } for day in _days(week_index, days)]


def _sunday(week_index):
    return (MONDAY + timedelta(days=7 * week_index + 6)).isoformat()


def _stable_control(weeks=4):
    rows = []
    for w in range(weeks):
        rows += _facts("999", w, 1000.0, 20, direction="med")
    return rows


# --------------------------------- weekly_pair_observations


def test_weekly_jump_with_rising_cpl_gives_positive_eps():
    # Кампания удвоила недельный бюджет, лиды выросли слабее (CPL 50 → 66.7),
    # контроль стоит на месте: eps > 0 — насыщение.
    facts = (_stable_control()
             + _facts("111", 0, 100.0, 2) + _facts("111", 1, 100.0, 2)
             + _facts("111", 2, 200.0, 3) + _facts("111", 3, 200.0, 3))
    obs, stats = weekly_pair_observations(facts, _sunday(3))
    assert len(obs["111"]) == 1
    assert obs["111"][0]["eps"] > 0
    assert stats["pairs_used"] == 1


def test_small_weekly_change_gives_no_observation():
    facts = (_stable_control()
             + _facts("111", 0, 100.0, 2) + _facts("111", 1, 110.0, 2)
             + _facts("111", 2, 115.0, 2) + _facts("111", 3, 118.0, 2))
    obs, stats = weekly_pair_observations(facts, _sunday(3))
    assert obs == {}
    assert stats["pairs_used"] == 0


def test_pair_containing_quasi_experiment_is_skipped():
    # Тот же скачок уже посчитан квазиэкспериментом 14-дневными окнами —
    # недельная пара обязана уступить, иначе скачок войдёт в свод дважды.
    facts = (_stable_control()
             + _facts("111", 0, 100.0, 2) + _facts("111", 1, 100.0, 2)
             + _facts("111", 2, 200.0, 3) + _facts("111", 3, 200.0, 3))
    change_day = (MONDAY + timedelta(days=14)).isoformat()
    obs, stats = weekly_pair_observations(
        facts, _sunday(3), {"111": [change_day]})
    assert obs == {}
    assert stats["pairs_quasi_overlap"] == 1


def test_immature_tail_week_is_dropped():
    # Скачки в непересекающихся парах (0,1) и (2,3): пары не делят недель.
    facts = (_stable_control()
             + _facts("111", 0, 100.0, 2) + _facts("111", 1, 200.0, 3)
             + _facts("111", 2, 100.0, 2) + _facts("111", 3, 400.0, 4))
    mature = weekly_pair_observations(facts, _sunday(3))[0]
    # Зрелость кончается в субботу последней недели — её пара исчезает.
    saturday = (MONDAY + timedelta(days=7 * 3 + 5)).isoformat()
    cut = weekly_pair_observations(facts, saturday)[0]
    assert len(mature["111"]) == 2
    assert len(cut["111"]) == 1


def test_partial_first_week_is_dropped():
    # Окно фактов начинается со среды: обрезанная неделя занизила бы бюджет
    # и дала бы фиктивный «скачок» к первой полной.
    wednesday = (MONDAY + timedelta(days=2)).isoformat()
    facts = [r for r in (_stable_control() + _facts("111", 0, 200.0, 3))
             if r["fact_date"] >= wednesday]
    facts += (_facts("111", 1, 100.0, 2) + _facts("111", 2, 100.0, 2)
              + _facts("111", 3, 100.0, 2)
              )
    obs, _ = weekly_pair_observations(facts, _sunday(3))
    assert obs == {}


def test_week_without_leads_gives_no_observation():
    facts = (_stable_control()
             + _facts("111", 0, 100.0, 2) + _facts("111", 1, 100.0, 2)
             + _facts("111", 2, 200.0, 0) + _facts("111", 3, 200.0, 0))
    obs, stats = weekly_pair_observations(facts, _sunday(3))
    assert obs == {}
    assert stats["pairs_used"] == 0


# --------------------------------- saturation_curves


def _quasi(campaign_id="111", effect=0.4, before=1000.0, after=2000.0,
           rel_error=0.1, started_on=None):
    return {
        "object_id": campaign_id,
        "effect": effect,
        "started_on": started_on or (MONDAY - timedelta(days=60)).isoformat(),
        "params": {"before": before, "after": after, "rel_error": rel_error},
    }


def _flat(campaign_id, direction, cost_per_day=100.0, leads_per_day=4):
    rows = []
    for w in range(4):
        rows += _facts(campaign_id, w, cost_per_day, leads_per_day, direction)
    return rows


def test_marginal_cpl_is_average_cpl_over_beta():
    facts = _flat("111", "spo")
    section = saturation_curves(facts, [_quasi("111")], {"111": "spo"}, _sunday(3))
    row = section["campaigns"]["111"]
    eps = 0.4 / math.log(2)
    assert abs(row["eps"] - eps) < 1e-3
    assert abs(row["beta"] - (1 - eps)) < 1e-3
    # 28 зрелых дней по 100 ₽ и 4 лида: CPL 25, предельная цена 25/β.
    assert row["cpl_28d"] == 25.0
    assert abs(row["marginal_cpl"] - 25.0 / (1 - eps)) < 0.05
    assert row["eps_source"] == "campaign"


def test_campaign_without_own_signal_borrows_direction_pool():
    facts = _flat("111", "spo") + _flat("222", "spo")
    section = saturation_curves(facts, [_quasi("111")], {"111": "spo", "222": "spo"},
                                _sunday(3))
    borrowed = section["campaigns"]["222"]
    assert borrowed["eps_source"] == "direction"
    assert abs(borrowed["eps"] - section["campaigns"]["111"]["eps"]) < 1e-6
    assert "spo" in section["directions"]


def test_direction_without_signal_borrows_account_pool():
    facts = _flat("111", "spo") + _flat("333", "vpo")
    section = saturation_curves(facts, [_quasi("111")], {"111": "spo", "333": "vpo"},
                                _sunday(3))
    assert section["campaigns"]["333"]["eps_source"] == "account"


def test_extreme_elasticity_is_clamped_and_flagged():
    # eps ≈ 1.3: сырая β ушла бы в минус, а предельная цена — в бесконечность.
    section = saturation_curves(
        _flat("111", "spo"), [_quasi("111", effect=0.9)], {"111": "spo"}, _sunday(3))
    row = section["campaigns"]["111"]
    assert row["beta"] == BETA_MIN
    assert row["beta_clamped"] is True


def test_campaign_without_recent_volume_gets_no_curve():
    # Наблюдения есть, но в свежем зрелом окне кампания не тратила: предельную
    # цену не к чему прикладывать — счётчик, а не молчание.
    old = []
    for w in range(4):
        old += _facts("111", w, 100.0, 4)
    later_sunday = _sunday(8)
    section = saturation_curves(old, [_quasi("111")], {"111": "spo"}, later_sunday)
    assert section["campaigns"] == {}
    assert section["campaigns_no_recent_volume"] == 1


def test_computed_rows_carry_beta_and_marginal_price():
    section = saturation_curves(
        _flat("111", "spo"), [_quasi("111")], {"111": "spo"}, _sunday(3))
    rows = computed_rows(section)["111"]
    by_key = {r["setting_key"]: r for r in rows}
    assert set(by_key) == {"beta", "marginal_cpl"}
    assert by_key["beta"]["value"] == section["campaigns"]["111"]["beta"]
    assert by_key["beta"]["support_n"] == section["campaigns"]["111"]["observations"]
    assert by_key["marginal_cpl"]["value"] == section["campaigns"]["111"]["marginal_cpl"]
    assert by_key["marginal_cpl"]["support_n"] == section["campaigns"]["111"]["leads_28d"]
    assert all(r["setting_kind"] == "saturation" for r in rows)


def test_weekly_pairs_do_not_share_a_week():
    # Пары (w1,w2) и (w2,w3) делят неделю: одно и то же наблюдение входит в
    # свод дважды, а свод считает их независимыми и фиктивно сужает ошибку.
    # Использованная пара забирает свои недели: следующая начинается с w3.
    facts = _stable_control(weeks=5)
    # Лиды меняются вместе с расходом: цена лида ровная, и фильтр возврата к
    # среднему (_week_is_rtm_suspect) в этот тест не вмешивается — проверяется
    # только то, что пары не делят неделю.
    for week, (cost, leads) in enumerate(
            ((100.0, 2), (300.0, 6), (100.0, 2), (300.0, 6), (100.0, 2))):
        facts += _facts("111", week, cost, leads)
    obs, stats = weekly_pair_observations(facts, _sunday(4))
    # Пять недель со скачком в каждой паре: непересекающихся пар две
    # (w0–w1 и w2–w3), а не четыре.
    assert stats["pairs_used"] == 2
    assert len(obs["111"]) == 2


def test_weekly_pair_after_a_cpl_spike_is_skipped_as_rtm():
    # Та же ловушка, что у квазиэкспериментов (mining.pre_trend_check): пара
    # недель, у которой неделя «до» сама выбивается из истории кампании,
    # меряет возврат к среднему, а не эффект бюджета.
    facts = _stable_control(weeks=5)
    facts += _facts("111", 0, 1000.0, 20)      # спокойная база: CPL 50
    facts += _facts("111", 1, 1000.0, 20)
    facts += _facts("111", 2, 1000.0, 5)       # всплеск CPL 200
    facts += _facts("111", 3, 500.0, 10)       # срезали бюджет, CPL вернулся
    facts += _facts("111", 4, 500.0, 10)
    obs, stats = weekly_pair_observations(facts, _sunday(4))
    assert stats["pairs_rtm_suspect"] >= 1
    assert "111" not in obs


def test_weekly_pair_on_calm_history_is_used():
    facts = _stable_control(weeks=5)
    for week in range(3):
        facts += _facts("111", week, 1000.0, 20)
    facts += _facts("111", 3, 2000.0, 30)      # скачок бюджета на ровной истории
    facts += _facts("111", 4, 2000.0, 30)
    obs, stats = weekly_pair_observations(facts, _sunday(4))
    assert stats["pairs_rtm_suspect"] == 0
    assert obs.get("111")


def test_weekly_pairs_respect_the_placebo_error_floor():
    # Тот же пол, что у квазиэкспериментов: пара недель не может быть точнее,
    # чем разброс DiD там, где ничего не менялось.
    facts = _stable_control(weeks=4)
    facts += _facts("111", 0, 1000.0, 20) + _facts("111", 1, 3000.0, 30)
    loose, _ = weekly_pair_observations(facts, _sunday(3))
    tight, _ = weekly_pair_observations(facts, _sunday(3), error_floor=0.5)
    log_jump = math.log(3.0)
    assert tight["111"][0]["rel_error"] >= 0.5 / log_jump - 1e-9
    assert tight["111"][0]["rel_error"] > loose["111"][0]["rel_error"]


# --------------------------------- недобор трафика в кривой


def _curves_with_headroom(headroom=None):
    """Кривая одной кампании с известной эластичностью + недобор трафика."""
    return saturation_curves(
        _flat("111", "spo"), [_quasi("111")], {"111": "spo"}, _sunday(3),
        headroom_by_campaign=headroom)


def test_curve_carries_traffic_headroom():
    section = _curves_with_headroom(
        {"111": {"traffic_volume": 45.0, "headroom_share": 0.55,
                 "verdict": "есть куда расти"}})
    curve = section["campaigns"]["111"]
    assert curve["traffic_volume"] == 45.0
    assert curve["headroom_share"] == 0.55
    assert curve["growth_room"] is True


def test_curve_without_headroom_data_says_none_not_false():
    # Отсутствие замера и «замерили, места нет» — разные вещи. False здесь
    # означал бы «расти некуда», а мы просто не знаем.
    curve = _curves_with_headroom()["campaigns"]["111"]
    assert curve["growth_room"] is None
    assert curve["traffic_volume"] is None


def test_curve_undetermined_headroom_is_none_not_false():
    # Сетевая кампания: объём приходит константой 100, вердикта нет. Записать
    # ей growth_room = False значит объявить «расти некуда» по величине,
    # которой никто не мерил.
    section = _curves_with_headroom(
        {"111": {"traffic_volume": None, "headroom_share": None,
                 "verdict": "неопределённо"}})
    curve = section["campaigns"]["111"]
    assert curve["growth_room"] is None
    assert curve["headroom_share"] is None


def test_bought_out_campaign_has_no_growth_room():
    section = _curves_with_headroom(
        {"111": {"traffic_volume": 95.0, "headroom_share": 0.05,
                 "verdict": "выкуплен"}})
    assert section["campaigns"]["111"]["growth_room"] is False


def test_direction_rows_carry_no_headroom():
    # Недобор не складывается по направлению линейно, а среднее по чужому
    # весу — та же ошибка «величина посчитана по чужой популяции».
    section = _curves_with_headroom(
        {"111": {"traffic_volume": 45.0, "headroom_share": 0.55,
                 "verdict": "есть куда расти"}})
    for row in section["directions"].values():
        assert row["traffic_volume"] is None
        assert row["growth_room"] is None
