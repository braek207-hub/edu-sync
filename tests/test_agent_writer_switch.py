# -*- coding: utf-8 -*-
"""
tests/test_agent_writer_switch.py — рычаг выключения кампаний (Э3.4):
план, diff, потолок, API-форма, рельсы, путь возврата.
"""

from sync.agent.writer.apply import to_api_call
from sync.agent.writer.guardrails import check_action, check_rollback
from sync.agent.writer.rollback import rollback_payload
from sync.agent.writer.switch import (
    MAX_SUSPENDS_PER_RUN,
    NOT_ON_REASON,
    cap_suspends,
    diff_switch,
    plan_switch_offs,
)
from sync.agent_e1_watchdog import rollback_guard_form


def _switch_row(roi_share, rel_error=0.05, calc_date="2026-08-23"):
    return {"setting_kind": "campaign_switch", "setting_key": "suspend",
            "value": roi_share, "raw_value": roi_share * 4.0, "support_n": 100,
            "rel_error": rel_error, "calc_date": calc_date}


# ----------------------------------------------------------------- план


def test_plan_takes_confident_candidate():
    plan = plan_switch_offs({"1": [_switch_row(0.3)]})
    assert "1" in plan["desired"]
    assert plan["desired"]["1"]["roi_share"] == 0.3
    assert plan["desired"]["1"]["calc_date"] == "2026-08-23"


def test_plan_rejects_low_confidence():
    # roi_share 0.9 с ошибкой 0.5: z мал, p_sign далеко от 0.97.
    plan = plan_switch_offs({"1": [_switch_row(0.9, rel_error=0.5)]})
    assert not plan["desired"]
    assert len(plan["low_confidence"]) == 1


def test_plan_refuses_unknown_confidence():
    # У бюджета строка без rel_error — допуск со счётчиком. Здесь наоборот:
    # выключение по числу без меры точности не планируется.
    plan = plan_switch_offs({"1": [_switch_row(0.3, rel_error=None)]})
    assert not plan["desired"]
    assert plan["confidence_unknown"] == 1


def test_plan_ignores_rows_of_other_kinds():
    rows = [{"setting_kind": "budget_target", "setting_key": "target_28d",
             "value": 100.0, "raw_value": 200.0, "support_n": 10,
             "rel_error": 0.1}]
    plan = plan_switch_offs({"1": rows})
    assert not plan["desired"]
    assert plan["confidence_unknown"] == 0


# ----------------------------------------------------------------- diff


def _move(roi_share=0.3, calc_date="2026-08-23"):
    return {"roi_share": roi_share, "roi_at_floor": roi_share * 4.0,
            "calc_date": calc_date, "p_sign": 0.999}


def test_on_campaign_builds_suspend_action():
    actions, refused = diff_switch({"1": _move()}, {"1": "ON"})
    assert not refused
    a = actions[0]
    assert a["action_kind"] == "campaign.suspend"
    assert a["payload"] == {"CampaignId": 1}
    assert a["previous_state"] == {"State": "ON"}
    assert a["direct_type"] == "CAMPAIGN_STATE" and a["key"] == "suspend"


def test_not_on_campaign_refused_with_state():
    actions, refused = diff_switch({"1": _move()}, {"1": "SUSPENDED"})
    assert not actions
    assert refused[0]["reason"] == NOT_ON_REASON.format(state="SUSPENDED")


def test_missing_campaign_neither_action_nor_refusal():
    actions, refused = diff_switch({"1": _move()}, {})
    assert not actions and not refused


def test_actions_ordered_worst_economy_first():
    desired = {"1": _move(roi_share=0.5), "2": _move(roi_share=0.1)}
    actions, _ = diff_switch(desired, {"1": "ON", "2": "ON"})
    assert [a["object_id"] for a in actions] == ["2", "1"]


def test_idempotency_key_changes_with_calc_date():
    a1, _ = diff_switch({"1": _move(calc_date="2026-08-23")}, {"1": "ON"})
    a2, _ = diff_switch({"1": _move(calc_date="2026-08-30")}, {"1": "ON"})
    # Человек мог включить кампанию обратно: новый расчёт — новое решение,
    # вечный ключ закрыл бы повторное выключение навсегда.
    assert a1[0]["idempotency_key"] != a2[0]["idempotency_key"]


def test_cap_allows_single_suspend_per_run():
    desired = {"1": _move(roi_share=0.1), "2": _move(roi_share=0.5)}
    actions, _ = diff_switch(desired, {"1": "ON", "2": "ON"})
    allowed, deferred = cap_suspends(actions)
    assert MAX_SUSPENDS_PER_RUN == 1
    assert [a["object_id"] for a in allowed] == ["1"]
    assert [a["object_id"] for a in deferred] == ["2"]


# ------------------------------------------------------------ API-формы


def _sample_action():
    actions, _ = diff_switch({"7": _move()}, {"7": "ON"})
    return actions[0]


def test_to_api_call_uses_selection_criteria():
    service, method, params = to_api_call(_sample_action())
    assert (service, method) == ("campaigns", "suspend")
    assert params == {"SelectionCriteria": {"Ids": [7]}}


# ---------------------------------------------------------------- рельсы


def test_check_action_passes_valid_suspend():
    ok, reason = check_action(_sample_action())
    assert ok, reason


def test_check_action_requires_previous_on():
    action = _sample_action()
    action["previous_state"] = {"State": "SUSPENDED"}
    ok, reason = check_action(action)
    assert not ok
    assert "ON" in reason


def test_check_action_requires_campaign_id():
    action = _sample_action()
    action["payload"] = {}
    ok, _ = check_action(action)
    assert not ok


# ------------------------------------------------------------------ откат


def test_rollback_is_resume():
    request = rollback_payload(_sample_action())
    assert request is not None
    service, method, params = request
    assert (service, method) == ("campaigns", "resume")
    assert params == {"SelectionCriteria": {"Ids": [7]}}


def test_rollback_without_on_previous_is_none():
    action = _sample_action()
    action["previous_state"] = {}
    assert rollback_payload(action) is None


def test_rollback_passes_return_guard():
    action = _sample_action()
    service, method, params = rollback_payload(action)
    form = rollback_guard_form(action, service, method, params)
    # Вид выведен из СОДЕРЖИМОГО запроса (resume + SelectionCriteria).
    assert form["action_kind"] == "campaign.suspend"
    ok, reason = check_rollback(form)
    assert ok, reason


def test_suspend_shaped_request_rejected_on_return_path():
    # Повторное выключение под видом отката: вид выводится как
    # campaigns.suspend и не проходит allow-лист возврата.
    form = rollback_guard_form(
        {"object_id": "7"}, "campaigns", "suspend",
        {"SelectionCriteria": {"Ids": [7]}})
    ok, _ = check_rollback(form)
    assert not ok
