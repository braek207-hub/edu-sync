# -*- coding: utf-8 -*-
"""
tests/test_agent_writer_diff.py — тесты разницы желаемого и фактического
состояния корректировок ставок.
"""

from sync.agent.writer.diff import diff_modifiers

DESIRED = [{"kind": "bid_modifier:device", "direct_type": "MOBILE_ADJUSTMENT",
            "key": "mobile", "percent": 30}]


def test_creates_add_when_modifier_absent():
    actions = diff_modifiers(DESIRED, actual=[], campaign_id="111")
    assert len(actions) == 1
    assert actions[0]["action_kind"] == "bidmodifier.add"
    assert actions[0]["previous_state"] == {}


def test_creates_set_when_value_differs():
    actual = [{"Id": 7, "Type": "MOBILE_ADJUSTMENT", "key": "mobile", "percent": 10}]
    actions = diff_modifiers(DESIRED, actual, campaign_id="111")
    assert len(actions) == 1
    assert actions[0]["action_kind"] == "bidmodifier.set"
    # Прошлое состояние обязано сохраниться — без него откат невозможен.
    assert actions[0]["previous_state"]["percent"] == 10
    assert actions[0]["payload"]["Id"] == 7


def test_no_action_when_already_matches():
    actual = [{"Id": 7, "Type": "MOBILE_ADJUSTMENT", "key": "mobile", "percent": 30}]
    assert diff_modifiers(DESIRED, actual, campaign_id="111") == []


def test_idempotency_key_is_stable():
    a = diff_modifiers(DESIRED, [], campaign_id="111")[0]["idempotency_key"]
    b = diff_modifiers(DESIRED, [], campaign_id="111")[0]["idempotency_key"]
    assert a == b


def test_idempotency_key_differs_per_campaign():
    a = diff_modifiers(DESIRED, [], campaign_id="111")[0]["idempotency_key"]
    b = diff_modifiers(DESIRED, [], campaign_id="222")[0]["idempotency_key"]
    assert a != b


def test_idempotency_key_differs_per_value():
    other = [{**DESIRED[0], "percent": 40}]
    a = diff_modifiers(DESIRED, [], campaign_id="111")[0]["idempotency_key"]
    b = diff_modifiers(other, [], campaign_id="111")[0]["idempotency_key"]
    assert a != b


def test_never_emits_delete():
    # Агент не удаляет объекты никогда: только add/set.
    actual = [{"Id": 7, "Type": "MOBILE_ADJUSTMENT", "key": "tablet", "percent": 50}]
    actions = diff_modifiers(DESIRED, actual, campaign_id="111")
    assert all(a["action_kind"] in {"bidmodifier.add", "bidmodifier.set"} for a in actions)
