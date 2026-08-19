# -*- coding: utf-8 -*-
from sync.agent.writer.guardrails import cap_actions, check_action, check_holdout


def _action(kind="bidmodifier.set", percent=30, object_id="111"):
    return {"action_kind": kind, "object_level": "campaign", "object_id": object_id,
            "payload": {"BidModifier": percent}}


def test_allows_normal_modifier():
    ok, reason = check_action(_action())
    assert ok is True
    assert reason == ""


def test_forbids_any_delete():
    ok, reason = check_action(_action(kind="bidmodifier.delete"))
    assert ok is False
    assert "удал" in reason.lower()


def test_forbids_delete_uppercase():
    ok, reason = check_action(_action(kind="bidmodifier.DELETE"))
    assert ok is False
    assert "удал" in reason.lower()


def test_forbids_delete_mixed_case():
    ok, reason = check_action(_action(kind="BidModifier.Delete"))
    assert ok is False
    assert "удал" in reason.lower()


def test_forbids_action_kind_outside_allowlist():
    ok, reason = check_action(_action(kind="purge"))
    assert ok is False
    assert "allow" in reason.lower()

    ok, reason = check_action(_action(kind="campaign.archive"))
    assert ok is False
    assert "allow" in reason.lower()


def test_forbids_modifier_beyond_cap():
    ok, reason = check_action(_action(percent=250))
    assert ok is False
    assert "потолок" in reason.lower()


def test_forbids_modifier_below_floor():
    ok, reason = check_action(_action(percent=-95))
    assert ok is False


def test_holdout_campaigns_are_untouched():
    actions = [_action(object_id="111"), _action(object_id="222")]
    allowed, blocked = check_holdout(actions, holdout_ids={"222"})
    assert [a["object_id"] for a in allowed] == ["111"]
    assert [a["object_id"] for a in blocked] == ["222"]


def test_holdout_protects_with_numeric_ids():
    actions = [_action(object_id="111"), _action(object_id="222")]
    allowed, blocked = check_holdout(actions, holdout_ids={222})
    assert [a["object_id"] for a in allowed] == ["111"]
    assert [a["object_id"] for a in blocked] == ["222"]


def test_cap_limits_actions_per_run():
    actions = [_action(object_id=str(i)) for i in range(80)]
    applied, deferred = cap_actions(actions, max_per_run=50)
    assert len(applied) == 50
    assert len(deferred) == 30


def test_cap_keeps_all_when_under_limit():
    actions = [_action() for _ in range(3)]
    applied, deferred = cap_actions(actions, max_per_run=50)
    assert len(applied) == 3
    assert deferred == []
