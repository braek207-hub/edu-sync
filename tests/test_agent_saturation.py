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
    facts = (_stable_control()
             + _facts("111", 0, 100.0, 2) + _facts("111", 1, 100.0, 2)
             + _facts("111", 2, 200.0, 3) + _facts("111", 3, 400.0, 4))
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
