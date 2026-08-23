# -*- coding: utf-8 -*-
"""Э2.4: квазиэксперименты → сигнал насыщения по кампаниям и направлениям."""

import math

from sync.agent.history import MIN_LOG_JUMP, budget_response, combine, elasticity


def _exp(campaign_id="111", effect=0.3, before=1000.0, after=2000.0,
         rel_error=0.1):
    return {
        "object_id": campaign_id,
        "effect": effect,
        "params": {"before": before, "after": after, "rel_error": rel_error},
    }


# --------------------------------- elasticity


def test_budget_up_cpl_up_is_positive_saturation_signal():
    obs = elasticity(_exp(effect=0.3, before=1000.0, after=2000.0))
    assert obs["eps"] > 0


def test_budget_cut_cpl_down_is_the_same_saturation_signal():
    # Срезали бюджет — лид подешевел: та же выпуклость кривой, что и
    # «долили — подорожал». Знак eps обязан совпадать.
    up = elasticity(_exp(effect=0.3, before=1000.0, after=2000.0))
    down = elasticity(_exp(effect=-0.3, before=2000.0, after=1000.0))
    assert up["eps"] > 0 and down["eps"] > 0


def test_tiny_jump_gives_no_elasticity():
    small = math.exp(MIN_LOG_JUMP / 2)
    assert elasticity(_exp(before=1000.0, after=1000.0 * small)) is None


def test_legacy_rows_without_rel_error_are_unusable():
    exp = _exp()
    del exp["params"]["rel_error"]
    assert elasticity(exp) is None


def test_elasticity_error_shrinks_with_bigger_jump():
    # Один и тот же эффект и та же ошибка измерения: чем крупнее скачок,
    # тем точнее делится на него эластичность.
    double = elasticity(_exp(before=1000.0, after=2000.0))
    quad = elasticity(_exp(before=1000.0, after=4000.0))
    assert quad["rel_error"] < double["rel_error"]


# --------------------------------- combine


def test_combine_weights_precise_observations_higher():
    pooled = combine([
        {"eps": 1.0, "rel_error": 0.05},
        {"eps": -1.0, "rel_error": 1.0},
    ])
    assert pooled["eps"] > 0.9
    assert pooled["n"] == 2


def test_combined_error_is_below_any_single_observation():
    pooled = combine([
        {"eps": 0.5, "rel_error": 0.3},
        {"eps": 0.4, "rel_error": 0.3},
    ])
    assert pooled["rel_error"] < 0.3


def test_combine_empty_is_none():
    assert combine([]) is None


# --------------------------------- budget_response


def test_confident_saturation_reaches_direction_level():
    experiments = [
        _exp("111", effect=0.5, rel_error=0.05),
        _exp("112", effect=0.4, rel_error=0.05),
    ]
    out = budget_response(experiments, {"111": "dist", "112": "dist"})
    assert out["campaigns"]["111"]["verdict"] == "насыщается"
    assert out["directions"]["dist"]["verdict"] == "насыщается"
    assert out["directions"]["dist"]["experiments"] == 2
    assert out["campaigns_confident"] == 2


def test_noisy_effect_is_undecided_not_a_verdict():
    # Порог класса budget_shift (0.90): шумная оценка не даёт вердикта,
    # даже когда точечный эффект велик.
    out = budget_response([_exp("111", effect=0.5, rel_error=2.0)], {"111": "spo"})
    assert out["campaigns"]["111"]["verdict"] == "неопределённо"
    assert out["campaigns_confident"] == 0


def test_unusable_experiments_are_counted_not_silent():
    exp = _exp("111")
    del exp["params"]["rel_error"]
    out = budget_response([exp], {"111": "spo"})
    assert out["experiments_unusable"] == 1
    assert out["campaigns_with_signal"] == 0


def test_campaign_without_direction_lands_in_unknown():
    out = budget_response([_exp("999", effect=0.5, rel_error=0.05)], {})
    assert "unknown" in out["directions"]


def test_direction_pools_across_campaigns_sharper_than_each():
    # Каждая кампания по отдельности шумит ниже порога, вместе — направление
    # получает вердикт. В этом ценность свода.
    experiments = [
        _exp(f"c{i}", effect=0.35, rel_error=0.4) for i in range(6)
    ]
    out = budget_response(experiments, {f"c{i}": "med" for i in range(6)})
    assert all(row["verdict"] == "неопределённо"
               for row in out["campaigns"].values())
    assert out["directions"]["med"]["verdict"] == "насыщается"
