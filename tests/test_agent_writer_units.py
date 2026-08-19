# -*- coding: utf-8 -*-
"""
tests/test_agent_writer_units.py — переходы между модулями движка записи.

Четыре Critical финального ревью ветки жили НЕ внутри модулей, а на стыках:
каждый модуль по отдельности корректен, 896 тестов зелёные, но по цепочке
«расчёт → план → diff → API» единицы измерения и ключи сегментов не сходились.
Поштучные тесты этого не видели, потому что каждый проверял свой модуль в его
собственных допущениях. Здесь тесты идут СКВОЗЬ цепочку — от строки
edu_agent_computed_settings до тела запроса к API.

Без сети и без БД: to_api_call/rollback_payload — чистые функции.
"""

import pytest

from sync.agent.computed import bid_modifier_percent
from sync.agent.writer.apply import to_api_call
from sync.agent.writer.diff import diff_modifiers
from sync.agent.writer.guardrails import MODIFIER_CAP, check_action
from sync.agent.writer.plan import plan_bid_modifiers
from sync.agent.writer.rollback import rollback_payload
from sync.agent.writer.units import API_NEUTRAL, api_to_delta, delta_to_api
from sync.agent_e1 import _normalize_actual


def _computed(kind, key, value, support=1000):
    return {"setting_kind": kind, "setting_key": key, "value": value, "support_n": support}


def _chain_to_api(kind, key, value, campaign_id="111"):
    """Сквозной прогон: вычисленная настройка → тело запроса к API."""
    desired = plan_bid_modifiers([_computed(kind, key, value)])["desired"]
    assert desired, "настройка не дошла до плана"
    actions = diff_modifiers(desired, actual=[], campaign_id=campaign_id)
    assert len(actions) == 1
    ok, reason = check_action(actions[0])
    assert ok, f"рельса отклонила действие: {reason}"
    return to_api_call(actions[0])


# ------------------------------------------------ единицы: дельта ↔ 100-база


def test_neutral_delta_is_hundred_not_zero():
    # Ноль в шкале Директа — «ставка × 0», максимальное подавление сегмента,
    # а не нейтраль. Нейтраль — 100.
    assert delta_to_api(0) == API_NEUTRAL == 100


def test_delta_to_api_matches_direct_convention():
    assert delta_to_api(30) == 130
    assert delta_to_api(-30) == 70


def test_api_to_delta_is_inverse():
    for delta in (-50, -20, 0, 15, 50):
        assert api_to_delta(delta_to_api(delta)) == delta


def test_delta_to_api_rejects_value_outside_direct_range():
    # Признак, что в границу конверсии пришло уже сконвертированное число
    # или число мимо рельсы: отправлять такое молча нельзя.
    with pytest.raises(ValueError):
        delta_to_api(-200)


# ---------------------------------------- дефект 1: дельта против 100-базы


def test_positive_delta_reaches_api_as_hundred_based_coefficient():
    # Э0 считает дельту (+30 %). Отправка «30» в API означала бы ставку 30 %
    # от исходной, то есть МИНУС семьдесят процентов — молча, без ошибки.
    _, _, params = _chain_to_api("bid_modifier:device", "MOBILE", 30.0)
    assert params["BidModifiers"][0]["MobileAdjustment"]["BidModifier"] == 130


def test_negative_delta_reaches_api_as_valid_coefficient():
    # «-20» в теле запроса вне допустимого диапазона → отказ элемента →
    # переприменение каждый прогон бесконечно. Должно уйти 80.
    _, _, params = _chain_to_api("bid_modifier:gender", "GENDER_MALE", -20.0)
    adjustment = params["BidModifiers"][0]["DemographicsAdjustments"][0]
    assert adjustment["BidModifier"] == 80


def test_set_action_also_converts_units():
    action = {"action_kind": "bidmodifier.set", "payload": {"Id": 7, "BidModifier": -20}}
    _, _, params = to_api_call(action)
    assert params["BidModifiers"][0] == {"Id": 7, "BidModifier": 80}


def test_computed_delta_is_the_unit_the_plan_receives():
    # Граница проведена так, что расчёт Э0 и план говорят на одном языке:
    # ratio 1.3 → дельта 30 → percent 30 (а не 130).
    assert bid_modifier_percent(1.3) == 30
    desired = plan_bid_modifiers([_computed("bid_modifier:device", "MOBILE", 30.0)])["desired"]
    assert desired[0]["percent"] == 30


def test_guardrail_is_calibrated_for_delta_scale():
    # Рельса работает по дельте: ±50 % от исходной ставки. 100-базное
    # значение, случайно попавшее в payload, обязано быть отклонено —
    # иначе рельса пропускала бы коридор 0..50, то есть разрешала бы
    # только срезание ставки вдвое и сильнее.
    assert MODIFIER_CAP == 50
    ok_delta, _ = check_action({"action_kind": "bidmodifier.set",
                                "payload": {"BidModifier": 50}})
    assert ok_delta is True
    ok_api, reason = check_action({"action_kind": "bidmodifier.set",
                                   "payload": {"BidModifier": 130}})
    assert ok_api is False and "потолок" in reason.lower()


def test_actual_state_is_normalized_back_to_delta():
    # Обратная граница: факт из API (130) сравнивается с планом (30) в одной
    # шкале, иначе diff переписывал бы корректировку каждый прогон.
    actual = _normalize_actual({"Id": 9, "MobileAdjustment": {"BidModifier": 130}})
    assert actual == [{"Id": 9, "Type": "MOBILE_ADJUSTMENT", "key": "MOBILE", "percent": 30}]

    desired = plan_bid_modifiers([_computed("bid_modifier:device", "MOBILE", 30.0)])["desired"]
    assert diff_modifiers(desired, actual, campaign_id="111") == []


# ------------------------------------ дефект 2: откат бьёт сильнее исходного


def test_rollback_of_add_sets_neutral_hundred_not_zero():
    # BidModifier=0 — не «нейтраль», а подавление сегмента: откат наносил бы
    # второй удар, сильнее первого.
    action = {"action_kind": "bidmodifier.add",
              "payload": {"CampaignId": 111, "Type": "MOBILE_ADJUSTMENT", "BidModifier": 30},
              "previous_state": {},
              "response": {"AddResults": [{"Id": 555}]}}
    _, method, params = rollback_payload(action)
    assert method == "set"
    assert params["BidModifiers"][0]["BidModifier"] == API_NEUTRAL == 100


def test_rollback_of_set_converts_previous_delta_to_api_scale():
    action = {"action_kind": "bidmodifier.set",
              "payload": {"Id": 7, "BidModifier": 30},
              "previous_state": {"Id": 7, "percent": -10}}
    _, _, params = rollback_payload(action)
    assert params["BidModifiers"][0]["BidModifier"] == 90


# -------------------------------------------- дефект 3: ключ устройства


def test_device_key_decides_adjustment_type():
    assert _chain_to_api("bid_modifier:device", "DESKTOP", 30.0)[2] \
        ["BidModifiers"][0]["DesktopAdjustment"]["BidModifier"] == 130
    assert _chain_to_api("bid_modifier:device", "TABLET", 30.0)[2] \
        ["BidModifiers"][0]["TabletAdjustment"]["BidModifier"] == 130


def test_desktop_never_travels_as_mobile_adjustment():
    # Ровно тот дефект: коэффициент, посчитанный для десктопа, уходил как
    # коэффициент смартфонов.
    _, _, params = _chain_to_api("bid_modifier:device", "DESKTOP", 30.0)
    assert "MobileAdjustment" not in params["BidModifiers"][0]


def test_unknown_device_is_reported_not_substituted():
    plan = plan_bid_modifiers([_computed("bid_modifier:device", "SMART_TV", 30.0)])
    assert plan["desired"] == []
    assert len(plan["unsupported"]) == 1
    assert plan["unsupported"][0]["key"] == "SMART_TV"
    assert plan["unsupported"][0]["reason"]


def test_actual_device_keys_match_plan_keys():
    # Reports API отдаёт устройства заглавными; план канонизирует ключ в
    # верхний регистр. Строчный "mobile" в нормализации факта не сходился с
    # планом никогда — diff вечно предлагал add вместо set.
    # По одной записи на устройство — так их и отдаёт bidmodifiers.get: у
    # Директа это три РАЗНЫХ объекта с разными Id, а не одна запись с тремя
    # полями (одна запись = один объект = одна нормализованная строка,
    # см. sync/agent_e1.py::_normalize_actual).
    normalized = []
    for id_, field, coefficient in ((9, "MobileAdjustment", 130),
                                    (10, "DesktopAdjustment", 120),
                                    (11, "TabletAdjustment", 90)):
        normalized += _normalize_actual({"Id": id_, field: {"BidModifier": coefficient}})
    by_type = {r["Type"]: r for r in normalized}
    assert by_type["MOBILE_ADJUSTMENT"]["key"] == "MOBILE"
    assert by_type["DESKTOP_ADJUSTMENT"]["key"] == "DESKTOP"
    assert by_type["TABLET_ADJUSTMENT"]["key"] == "TABLET"

    desired = plan_bid_modifiers([
        _computed("bid_modifier:device", "DESKTOP", 20.0),
    ])["desired"]
    # Факт уже совпадает с планом (120 в API = +20 дельты) — действий нет.
    assert diff_modifiers(desired, normalized, campaign_id="111") == []


def test_desktop_and_mobile_are_separate_actions():
    plan = plan_bid_modifiers([
        _computed("bid_modifier:device", "DESKTOP", 20.0),
        _computed("bid_modifier:device", "MOBILE", -20.0),
    ])
    types = {d["direct_type"] for d in plan["desired"]}
    assert types == {"DESKTOP_ADJUSTMENT", "MOBILE_ADJUSTMENT"}


# ----------------------------------------- дефект 4: региональные ключи


def test_region_name_never_reaches_api_call():
    # int("Москва") → ValueError → 'failed' → переприменение навсегда,
    # со съеданием слотов лимита действий.
    plan = plan_bid_modifiers([_computed("bid_modifier:region", "Москва", 30.0)])
    assert plan["desired"] == []
    assert len(plan["unsupported"]) == 1
    assert "регион" in plan["unsupported"][0]["reason"].lower()


def test_region_with_numeric_id_is_applied():
    # Как только срез начнёт отдавать RegionId, региональные корректировки
    # поедут сами — без новой правки кода.
    _, _, params = _chain_to_api("bid_modifier:region", "213", 30.0)
    adjustment = params["BidModifiers"][0]["RegionalAdjustments"][0]
    assert adjustment == {"RegionId": 213, "BidModifier": 130}


def test_region_name_does_not_raise_anywhere_in_chain():
    plan = plan_bid_modifiers([
        _computed("bid_modifier:region", "Санкт-Петербург", 30.0),
        _computed("bid_modifier:device", "MOBILE", 30.0),
    ])
    actions = diff_modifiers(plan["desired"], actual=[], campaign_id="111")
    for action in actions:
        to_api_call(action)   # не должно бросать
    assert len(actions) == 1
