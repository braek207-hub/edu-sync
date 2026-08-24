# -*- coding: utf-8 -*-
"""Э3.5: экономически допустимая цель CPA (расчётный слой).

Цель CPA в кабинете назначается за КОНВЕРСИЮ ЦЕЛИ Директа, а не за
эффективный лид CRM и не за оплату. Поэтому весь расчёт живёт в валюте
конверсий: ценность конверсии, предельная цена конверсии, недодержание цели.
"""

from sync.agent.tcpa import (
    DEFAULT_TARGET_ROMI,
    MIN_CONVERSIONS,
    tcpa_target,
    tcpa_targets,
)


def _campaign(**over):
    base = {
        "campaign_id": "111",
        "cost": 300_000.0,
        "conversions": 200.0,      # факт по цели 1500 ₽
        "revenue": 900_000.0,      # 4500 ₽ ценности на конверсию
        "beta": 0.8,
        "value_rel_error": 0.15,
        "tcpa_current": 1000.0,    # цель 1000, факт 1500 — недодержание ×1.5
    }
    base.update(over)
    return base


def test_target_is_marginal_value_corrected_for_slippage():
    # Допустимый ПРЕДЕЛЬНЫЙ CPA = ценность конверсии × β / требуемая
    # окупаемость: 4500 × 0.8 / 2 = 1800. Факт стоит в 1.5 раза выше цели,
    # поэтому цель ставится ниже допустимого факта: 1800 / 1.5 = 1200.
    out = tcpa_target(_campaign(), target_romi=2.0)
    assert abs(out["value_per_conversion"] - 4500.0) < 1e-6
    assert abs(out["allowed_marginal_cpa"] - 1800.0) < 1e-6
    assert abs(out["slippage"] - 1.5) < 1e-6
    assert abs(out["target"] - 1200.0) < 1e-6
    assert out["move"] == "up"


def test_saturated_campaign_gets_a_stricter_target():
    # Чем круче насыщение (меньше β), тем дешевле обязана быть предельная
    # конверсия: тот же доход при β=0.4 разрешает вдвое меньшую цель.
    flat = tcpa_target(_campaign(beta=0.8), target_romi=2.0)["target"]
    steep = tcpa_target(_campaign(beta=0.4), target_romi=2.0)["target"]
    assert abs(steep - flat / 2) < 1e-6


def test_unprofitable_campaign_is_told_to_go_down():
    out = tcpa_target(_campaign(revenue=200_000.0), target_romi=2.0)
    assert out["move"] == "down"
    assert out["target"] < out["tcpa_current"]


def test_thin_campaign_gets_no_target():
    out = tcpa_target(_campaign(conversions=MIN_CONVERSIONS - 1), target_romi=2.0)
    assert out["target"] is None
    assert "конверсий" in out["reason"]


def test_campaign_without_revenue_gets_no_target():
    out = tcpa_target(_campaign(revenue=0.0), target_romi=2.0)
    assert out["target"] is None


def test_confidence_measures_the_economic_edge():
    # Уверенность — в ЭКОНОМИЧЕСКОЙ гипотезе «допустимый предельный CPA выше
    # (ниже) фактического», а не в размере шага: тот же инвариант, что у
    # бюджетного рычага после аудита (C2).
    sure = tcpa_target(_campaign(value_rel_error=0.05), target_romi=2.0)
    noisy = tcpa_target(_campaign(value_rel_error=0.6), target_romi=2.0)
    assert sure["confident"] is True
    assert noisy["confident"] is False
    assert sure["roi_vs_target"] > 1.0


def test_section_splits_confident_moves_and_reasons():
    section = tcpa_targets([
        _campaign(campaign_id="1"),
        _campaign(campaign_id="2", revenue=200_000.0),
        _campaign(campaign_id="3", conversions=1.0),
    ], target_romi=DEFAULT_TARGET_ROMI)
    assert set(section["targets"]) == {"1", "2"}
    assert section["no_target"] == 1
    assert section["moves_up"] >= 1
