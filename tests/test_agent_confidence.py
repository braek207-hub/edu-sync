# -*- coding: utf-8 -*-
"""Э2.3: уверенность в знаке эффекта против порога класса действия."""

import pytest

from sync.agent.computed import ratio_rel_error
from sync.agent.confidence import ACTION_CLASSES, assess, p_sign
from sync.agent.writer.plan import plan_bid_modifiers


# --------------------------------- p_sign


def test_ratio_one_is_a_coin_flip():
    assert p_sign(1.0, 0.1) == pytest.approx(0.5)


def test_strong_effect_with_small_error_is_near_certain():
    assert p_sign(1.5, 0.05) > 0.99


def test_same_effect_with_huge_error_is_uncertain():
    assert p_sign(1.5, 2.0) < 0.6


def test_negative_effect_is_symmetric_to_positive():
    assert p_sign(0.8, 0.1) == pytest.approx(p_sign(1.25, 0.1))


def test_unknown_error_gives_none_not_confidence():
    assert p_sign(1.5, None) is None
    assert p_sign(1.5, 0.0) is None
    assert p_sign(0.0, 0.1) is None


# --------------------------------- assess: пороги классов


def test_irreversible_class_demands_more_certainty():
    # Одна и та же оценка: для обратимой корректировки — достаточно,
    # для остановки кампании — нет. В этом весь смысл классов.
    ratio, rel = 1.3, 0.2  # p_sign ≈ 0.905
    assert assess(ratio, rel, "bid_modifier")["confident"] is True
    assert assess(ratio, rel, "campaign_state")["confident"] is False


def test_missing_rel_error_is_unknown_not_yes():
    verdict = assess(1.3, None, "bid_modifier")
    assert verdict["confident"] is None
    assert verdict["p_sign"] is None


def test_unknown_action_class_is_refused_with_reason():
    verdict = assess(1.3, 0.1, "телепортация")
    assert verdict["confident"] is False
    assert "телепортация" in verdict["reason"]


def test_class_thresholds_are_ordered_by_reversibility():
    assert (ACTION_CLASSES["bid_modifier"]["min_p_sign"]
            < ACTION_CLASSES["budget_shift"]["min_p_sign"]
            < ACTION_CLASSES["campaign_state"]["min_p_sign"])


# --------------------------------- rel_error расчёта


def test_rel_error_shrinks_with_more_events():
    assert ratio_rel_error(400, 1000) < ratio_rel_error(25, 1000)


def test_zero_events_segment_gets_finite_error_upper_bound():
    # Ноль конверсий — сильный сигнал против сегмента, а не «неизвестно»:
    # бесконечная ошибка выкидывала бы самые убедительные минус-сегменты.
    rel = ratio_rel_error(0, 1000)
    assert rel is not None and rel < 1.1


def test_no_base_events_means_no_error_estimate():
    assert ratio_rel_error(10, 0) is None


# --------------------------------- гейт в плане корректировок


def _row(key, value, rel_error, support=1000):
    return {"setting_kind": "bid_modifier:device", "setting_key": key,
            "value": float(value), "support_n": support,
            "raw_value": 1.0 + value / 100.0, "rel_error": rel_error}


def test_plan_drops_low_confidence_rows_visibly():
    plan = plan_bid_modifiers([
        _row("MOBILE", 20.0, 0.05),   # p_sign ≈ 1 — применяем
        _row("DESKTOP", -15.0, 0.9),  # знак — монетка, не применяем
    ])
    assert [r["key"] for r in plan["desired"]] == ["MOBILE"]
    assert len(plan["low_confidence"]) == 1
    dropped = plan["low_confidence"][0]
    assert dropped["key"] == "DESKTOP"
    assert dropped["p_sign"] < ACTION_CLASSES["bid_modifier"]["min_p_sign"]


def test_plan_keeps_legacy_rows_without_rel_error_but_counts_them():
    plan = plan_bid_modifiers([_row("MOBILE", 20.0, None)])
    assert [r["key"] for r in plan["desired"]] == ["MOBILE"]
    assert plan["confidence_unknown"] == 1
    assert plan["low_confidence"] == []
