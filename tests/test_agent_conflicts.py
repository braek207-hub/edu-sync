# -*- coding: utf-8 -*-
"""Рычаги, тянущие одну кампанию в разные стороны.

Класс дефектов, который не ловится ни тестом рычага (рычаг прав), ни
журналом применённого (обе строки применились штатно): противоречие живёт
между двумя законными решениями и видно только на собранном плане.
"""

from sync.agent import conflicts


def _budget(cid="111", new=140_000_000, old=100_000_000):
    return {"object_level": "campaign", "object_id": cid,
            "direct_type": "WEEKLY_SPEND_LIMIT", "key": "search",
            "idempotency_key": f"b-{cid}-{new}",
            "payload": {"WeeklySpendLimit": new},
            "previous_state": {"WeeklySpendLimit": old}}


def _tcpa(cid="111", new=800_000_000, old=1_000_000_000):
    return {"object_level": "campaign", "object_id": cid,
            "direct_type": "AVERAGE_CPA", "key": "target_cpa",
            "idempotency_key": f"t-{cid}-{new}",
            "payload": {"TargetCpa": new},
            "previous_state": {"TargetCpa": old}}


def _suspend(cid="111"):
    return {"object_level": "campaign", "object_id": cid,
            "direct_type": "CAMPAIGN_STATE", "key": "suspend",
            "idempotency_key": f"s-{cid}",
            "payload": {"CampaignId": int(cid)},
            "previous_state": {"State": "ON"}}


def _modifier(cid="111", key="MOBILE"):
    return {"object_level": "campaign", "object_id": cid,
            "direct_type": "bid_modifier:device", "key": key,
            "idempotency_key": f"m-{cid}-{key}",
            "payload": {"Id": 1}, "previous_state": {}}


def test_money_up_and_target_down_is_a_conflict():
    # Даём кампании денег и тут же лишаем стратегию возможности их потратить.
    kept, dropped = conflicts.resolve([_budget(), _tcpa()])

    assert kept == []
    assert {a["conflict_reason"] for a in dropped} == {conflicts.OPPOSING_LEVERS}


def test_levers_pointing_the_same_way_pass():
    # Поднять лимит и поднять цель — согласованное решение, не конфликт.
    kept, dropped = conflicts.resolve([_budget(), _tcpa(new=1_200_000_000)])

    assert len(kept) == 2
    assert dropped == []


def test_suspension_cancels_everything_else_on_the_campaign():
    # Изменения выключаемой кампании не сделают ничего, но будут оплачены.
    kept, dropped = conflicts.resolve([_suspend(), _budget(), _modifier()])

    assert [a["direct_type"] for a in kept] == ["CAMPAIGN_STATE"]
    assert {a["conflict_reason"] for a in dropped} == {conflicts.SUSPENDED_OBJECT}


def test_suspension_does_not_touch_other_campaigns():
    kept, _ = conflicts.resolve([_suspend("111"), _budget("222")])

    assert len(kept) == 2


def test_second_action_on_the_same_segment_is_dropped():
    # Второе затирает первое, а наблюдение потом судит обоих по одному исходу.
    kept, dropped = conflicts.resolve([_modifier(key="MOBILE"),
                                       _modifier(key="MOBILE")])

    assert len(kept) == 1
    assert dropped[0]["conflict_reason"] == conflicts.DUPLICATE_SEGMENT


def test_different_segments_of_one_campaign_are_not_duplicates():
    kept, dropped = conflicts.resolve([_modifier(key="MOBILE"),
                                       _modifier(key="DESKTOP")])

    assert len(kept) == 2
    assert dropped == []


def test_unknown_direction_never_invents_a_conflict():
    # Ложный конфликт дороже пропущенного: из-за него встало бы законное
    # изменение. Величины в действии нет — направления нет.
    budget = _budget()
    budget["previous_state"] = {}

    kept, dropped = conflicts.resolve([budget, _tcpa()])

    assert len(kept) == 2
    assert dropped == []


def test_order_of_the_plan_is_preserved():
    # План уже отсортирован вызывающим по цене; менять приоритет здесь —
    # принимать решение, которого этому модулю не поручали.
    plan = [_modifier(key="A"), _modifier(key="B"), _modifier(key="C")]

    kept, _ = conflicts.resolve(plan)

    assert [a["key"] for a in kept] == ["A", "B", "C"]


def test_by_reason_counts_what_was_dropped():
    _, dropped = conflicts.resolve([_suspend(), _budget(), _modifier()])

    assert conflicts.by_reason(dropped) == {conflicts.SUSPENDED_OBJECT: 2}


def test_empty_plan_is_not_an_error():
    assert conflicts.resolve([]) == ([], [])
