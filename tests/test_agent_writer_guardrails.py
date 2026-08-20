# -*- coding: utf-8 -*-
from sync.agent.writer.guardrails import (
    cap_actions,
    check_action,
    check_holdout,
    check_rollback,
    expected_rollback_coefficient,
)
from sync.agent.writer.units import API_MAX, API_NEUTRAL


def _action(kind="bidmodifier.set", percent=30, object_id="111"):
    return {"action_kind": kind, "object_level": "campaign", "object_id": object_id,
            "payload": {"BidModifier": percent}}


_KEEP_IN_SYNC = object()   # «прошлое состояние не задано явно»


def _rollback(kind="bidmodifier.set", coefficient=110, object_id="111",
              origin="bidmodifier.set", previous=_KEEP_IN_SYNC):
    """Запрос на возврат в форме, которую рельса получает от сторожа.

    По умолчанию прошлое состояние согласовано с коэффициентом: тесты про
    диапазон и allow-лист проверяют своё, а не спотыкаются о сверку намерения.
    Рассогласование задаётся явно параметром previous.
    """
    if previous is _KEEP_IN_SYNC:
        previous = {"Id": 7, "percent": (coefficient or 0) - API_NEUTRAL}
    return {"action_kind": kind, "object_level": "campaign", "object_id": object_id,
            "api_coefficient": coefficient, "payload": {"Id": 7},
            "origin_action_kind": origin, "previous_state": previous}


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


# ------------------------------------------------ рельса пути возврата

def test_rollback_allows_legitimate_past_value_above_assignment_cap():
    # Человек когда-то поставил +80 % — штатное значение Директа. Потолок
    # НАЗНАЧЕНИЯ (±50 %) описывает, что позволено ставить агенту, и к возврату
    # к чужому прошлому значению отношения не имеет: пропущенный через него
    # откат отклонялся бы, а изменение агента оставалось в кабинете навсегда.
    ok, reason = check_rollback(_rollback(coefficient=180))
    assert ok is True
    assert reason == ""


def test_rollback_allows_disabled_segment_as_past_value():
    # Ноль в шкале Директа — «показы на устройстве выключены», обычная
    # настройка. В дельтах это -100 и вылет за потолок назначения.
    ok, _ = check_rollback(_rollback(coefficient=0))
    assert ok is True


def test_rollback_rejects_coefficient_outside_direct_range():
    ok, reason = check_rollback(_rollback(coefficient=API_MAX + 100))
    assert ok is False
    assert "диапазон" in reason.lower()


def test_rollback_rejects_delete():
    ok, reason = check_rollback(_rollback(kind="bidmodifier.delete"))
    assert ok is False
    assert "удал" in reason.lower()


def test_rollback_rejects_add_because_it_creates_a_new_object():
    # add на пути возврата — это не восстановление старого объекта, а создание
    # нового: ещё одно изменение кабинета под видом отмены.
    ok, reason = check_rollback(_rollback(kind="bidmodifier.add"))
    assert ok is False
    assert "allow" in reason.lower()


def test_rollback_rejects_request_without_coefficient():
    ok, reason = check_rollback(_rollback(coefficient=None))
    assert ok is False


# --------------------------- рельса возврата сверяет НАМЕРЕНИЕ, а не форму

def test_rollback_rejects_coefficient_that_is_not_the_past_value():
    # Диапазон API Директа — 0..1300, то есть рельса «по форме» пропускает
    # почти любое значение. Единственное, что связывает запрос с реальным
    # прошлым значением, — код, который его строит; ошибка в нём (предельное
    # значение вместо прежнего) шла бы прямо в боевой кабинет: путь возврата
    # больше ничем не проверен.
    ok, reason = check_rollback(
        _rollback(coefficient=150, previous={"Id": 7, "percent": 10}))
    assert ok is False
    assert "110" in reason and "прошл" in reason.lower()


def test_rollback_allows_coefficient_equal_to_the_past_value():
    ok, reason = check_rollback(
        _rollback(coefficient=110, previous={"Id": 7, "percent": 10}))
    assert ok is True
    assert reason == ""


def test_rollback_of_added_modifier_must_go_to_neutral():
    # Отмена добавления возвращает нейтраль: объекта до действия не было.
    ok, _ = check_rollback(_rollback(coefficient=API_NEUTRAL,
                                     origin="bidmodifier.add", previous={}))
    assert ok is True

    ok, reason = check_rollback(_rollback(coefficient=130,
                                          origin="bidmodifier.add", previous={}))
    assert ok is False
    assert "100" in reason


def test_rollback_rejects_when_past_value_is_unknown():
    # Прошлого коэффициента в журнале нет — сверять не с чем. Пропустить
    # «раз проверить нечем» означало бы отключать сверку ровно на тех
    # строках, где журнал испорчен.
    ok, reason = check_rollback(_rollback(previous={"Id": 7}))
    assert ok is False
    assert "previous_state" in reason


def test_rollback_rejects_unknown_origin_action_kind():
    ok, reason = check_rollback(_rollback(origin="campaign.pause"))
    assert ok is False
    assert "исходного действия" in reason


def test_rollback_rejects_unreadable_past_value_without_raising():
    ok, reason = check_rollback(_rollback(previous={"Id": 7, "percent": "много"}))
    assert ok is False
    assert "нечита" in reason.lower()


def test_expected_coefficient_is_derived_from_the_journal():
    assert expected_rollback_coefficient("bidmodifier.set", {"percent": 10})[0] == 110
    assert expected_rollback_coefficient("bidmodifier.set", {"percent": -100})[0] == 0
    assert expected_rollback_coefficient("bidmodifier.add", {})[0] == API_NEUTRAL
    assert expected_rollback_coefficient("bidmodifier.set", None)[0] is None


def test_rollback_refuses_non_numeric_coefficient_instead_of_raising():
    # Отказ обязан быть отказом: исключение отсюда вызывающий код
    # (agent_e1_watchdog.rollback_one) превращает в пометку «неоткатываемо
    # НАВСЕГДА» — то есть падение рельсы хоронит действие в кабинете.
    ok, reason = check_rollback(_rollback(coefficient="сто десять",
                                          previous={"Id": 7, "percent": 10}))
    assert ok is False
    assert "не число" in reason


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
