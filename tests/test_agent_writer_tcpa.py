# -*- coding: utf-8 -*-
"""Э3.5 (запись): рычаг целевого CPA.

Тот же конвейер, что у бюджета: план из computed-строк → разница с прочитанным
кабинетом → действие с previous_state и ключом идемпотентности. Отличие в
базе капа: у бюджета ею служит РАСХОД (лимит бывает декорацией), а цель CPA —
управляющая ручка, которая расход реально связывает, поэтому шаг капится от
самой цели.
"""

import pytest

from sync.agent.writer.tcpa import (
    MAX_TCPA_STEP,
    TCPA_KIND,
    diff_tcpa,
    plan_tcpa_moves,
    read_target_cpa,
    strategy_with_target,
    to_api_call,
)

M = 1_000_000


def _target_row(target, current, rel_error=0.05):
    return {"setting_kind": "tcpa_target", "setting_key": "target",
            "value": target, "raw_value": current, "support_n": 200,
            "rel_error": rel_error}


def _roi_row(roi, rel_error=0.05):
    return {"setting_kind": "tcpa_target", "setting_key": "roi_vs_target",
            "value": roi, "raw_value": 1500.0, "support_n": 200,
            "rel_error": rel_error}


def _strategy(cpa_micros, channel="Search"):
    return {
        channel: {"BiddingStrategyType": "AVERAGE_CPA",
                  "AverageCpa": {"AverageCpa": cpa_micros,
                                 "WeeklySpendLimit": 350_000 * M,
                                 "GoalId": 360811375}},
        "Network": {"BiddingStrategyType": "SERVING_OFF"} if channel == "Search" else {},
    }


def _state(cpa_micros, campaign_type="TEXT_CAMPAIGN", package_id=None):
    return {"strategy": _strategy(cpa_micros), "package_id": package_id,
            "campaign_type": campaign_type}


# ------------------------------------------------------------------ план


def test_plan_takes_confident_economic_edge():
    plan = plan_tcpa_moves({"1": [_target_row(1300.0, 1000.0), _roi_row(1.4)]})
    assert plan["desired"]["1"]["target"] == 1300.0
    assert plan["desired"]["1"]["current"] == 1000.0


def test_plan_refuses_without_economic_row():
    plan = plan_tcpa_moves({"1": [_target_row(1300.0, 1000.0)]})
    assert plan["desired"] == {}
    assert plan["confidence_unknown"] == 1


def test_plan_gates_on_noisy_edge():
    plan = plan_tcpa_moves({"1": [_target_row(1300.0, 1000.0, rel_error=0.5),
                                  _roi_row(1.05, rel_error=0.5)]})
    assert plan["desired"] == {}
    assert plan["low_confidence"][0]["campaign_id"] == "1"


def test_plan_skips_small_shift():
    plan = plan_tcpa_moves({"1": [_target_row(1020.0, 1000.0), _roi_row(1.4)]})
    assert plan["desired"] == {}
    assert plan["small_shift"] == 1


# ------------------------------------------------------------- разница


def test_step_is_capped_by_the_current_target():
    # Цель — ручка, которая расход реально связывает: кап считается от неё.
    actions, refused = diff_tcpa({"1": {"target": 2000.0, "current": 1000.0}},
                                 {"1": _state(1000 * M)})
    assert not refused
    payload = actions[0]["payload"]
    assert read_target_cpa(payload["BiddingStrategy"]) == int(
        round(1000.0 * (1 + MAX_TCPA_STEP))) * M


def test_already_set_produces_nothing():
    actions, refused = diff_tcpa({"1": {"target": 1200.0, "current": 1000.0}},
                                 {"1": _state(1200 * M)})
    assert not actions and not refused


def test_package_strategy_is_refused():
    actions, refused = diff_tcpa({"1": {"target": 1200.0, "current": 1000.0}},
                                 {"1": _state(1000 * M, package_id=708062738)})
    assert not actions and refused


def test_strategy_without_target_cpa_is_refused():
    state = {"strategy": {"Search": {"BiddingStrategyType": "WB_MAXIMUM_CLICKS"}},
             "package_id": None, "campaign_type": "TEXT_CAMPAIGN"}
    actions, refused = diff_tcpa({"1": {"target": 1200.0, "current": 1000.0}},
                                 {"1": state})
    assert not actions and refused


def test_action_carries_previous_state_and_idempotency_key():
    actions, _ = diff_tcpa({"1": {"target": 1150.0, "current": 1000.0}},
                           {"1": _state(1000 * M)})
    action = actions[0]
    assert action["action_kind"] == TCPA_KIND
    assert action["object_level"] == "campaign"
    assert read_target_cpa(action["previous_state"]["BiddingStrategy"]) == 1000 * M
    assert action["idempotency_key"]
    assert action["direct_type"] == "AVERAGE_CPA"


def test_strategy_with_target_keeps_neighbouring_settings():
    # Блок уходит в update целиком: соседние настройки (недельный лимит, цель
    # конверсии) обязаны пережить правку.
    updated = strategy_with_target(_strategy(1000 * M), 1300 * M)
    holder = updated["Search"]["AverageCpa"]
    assert holder["AverageCpa"] == 1300 * M
    assert holder["WeeklySpendLimit"] == 350_000 * M
    assert holder["GoalId"] == 360811375


def test_to_api_call_is_a_campaign_update():
    actions, _ = diff_tcpa({"1": {"target": 1150.0, "current": 1000.0}},
                           {"1": _state(1000 * M)})
    service, method, params = to_api_call(actions[0])
    assert (service, method) == ("campaigns", "update")
    campaign = params["Campaigns"][0]
    assert campaign["Id"] == 1
    assert read_target_cpa(campaign["TextCampaign"]["BiddingStrategy"]) == 1150 * M
