# -*- coding: utf-8 -*-
"""
tests/test_agent_writer_audience.py — рычаг аудиторий и ретаргетинга (задача 23).

Корректировка на сегмент ретаргетинга — это та же ручка ставки, что у пола,
возраста и устройств: обучение стратегии она не сбрасывает, деньги переносит
ВНУТРИ кампании и меряется недельным горизонтом. Отсюда полоса тонкой
настройки, а не перераспределения: полоса 3 — про то, КУДА кампания тратит
свой бюджет, и её ошибка стоит недель переобучения; здесь ошибка стоит доли
сегмента за неделю.

Второе, что проверяется здесь, — цена незнания. Замер 26.08.2026 по четырём
кабинетам (probe_retargeting_lever.py, docs/AGENT-EXPERIMENT-PIPELINE.md)
показал: форма запроса верна, а привязок условий к группам НОЛЬ. Рычаг,
двигающий ставку по сегменту, которого нет в показах, исправно предлагал бы
действия, не меняющие ничего, — поэтому непривязанный и непрочитанный
сегменты отказываются, а не считаются пустыми.
"""

import pytest

from sync.agent.writer import (audience as audience_mod, exposure, expectation,
                               guardrails, lanes, learning, tier)
from sync.agent.writer.apply import to_api_call
from sync.agent.writer.diff import bidmod_idempotency_key

CAMPAIGN = "111"
CONDITION = 4_400_001
OTHER_CONDITION = 4_400_002


def _candidate(condition_id=CONDITION, percent=30, share=0.12,
               attached=3, **over):
    item = {"condition_id": condition_id, "percent": percent, "share": share,
            "attached_ad_groups": attached, "segment_name": "Купившие 90 дн."}
    item.update(over)
    return item


def _desired(candidates=None, **over):
    move = {"audiences": candidates if candidates is not None else [_candidate()],
            "daily_cost_rub": 12_000.0, "cpa_rub": 2_400.0}
    move.update(over)
    return {CAMPAIGN: move}


def _state(modifiers=None, **over):
    """Прочитанное состояние кампании: корректировки на ретаргетинг.

    Ключ присутствует всегда, даже пустым списком: «корректировок нет» и
    «корректировки не читали» — разные утверждения, и рычаг обязан их
    различать.
    """
    state = {"retargeting_modifiers": [] if modifiers is None else modifiers}
    state.update(over)
    return {CAMPAIGN: state}


def _diff(desired=None, state=None):
    return audience_mod.diff_audience(desired or _desired(), state or _state())


def _action(desired=None, state=None):
    actions, refused = _diff(desired, state)
    assert refused == [], refused
    return actions[0]


# ------------------------------------------- полоса тонкой настройки (шаг 1)


def test_the_audience_adjustment_lives_in_the_tuning_lane():
    """Шаг 1 задачи 23.

    Полоса — не ярлык: из неё берутся горизонт замера, риск-доля и лимит
    объектов за прогон. Попади корректировка сегмента в полосу
    перераспределения, её мерили бы четырнадцатью днями вместо семи и
    оплачивали бы из кармана, рассчитанного под сдвиги бюджета.
    """
    action = _action()
    assert lanes.lane_of(action) == lanes.LANE_TUNING
    assert lanes.lane_of(action) != lanes.LANE_ALLOCATION
    assert (expectation.of(action, {})["measure_days"]
            == lanes.MEASURE_DAYS[lanes.LANE_TUNING])


def test_the_audience_adjustment_does_not_reset_learning():
    # Справка Директа перечисляет сбрасывающие изменения поимённо (стратегия,
    # модель атрибуции и оплаты, ограничение расхода, корректировка ЦЕЛЕВЫХ
    # ДЕЙСТВИЙ, остановка дольше семи дней). Корректировки ставки в списке
    # нет — ни у устройств, ни у аудиторий.
    assert audience_mod.AUDIENCE_KIND in learning.SAFE_FOR_LEARNING
    assert learning.learning_impact(_action()) == "safe"


# ------------------------------------------- одна аудитория на такт (шаг 2)


def test_only_one_audience_per_campaign_per_tick():
    """Шаг 2 задачи 23.

    Две корректировки на одну кампанию в один такт замеряются одним исходом:
    разделить их вклад потом нечем, и обе списывают риск-бюджет.
    """
    actions, refused = _diff(_desired([
        _candidate(CONDITION, percent=10, share=0.05),
        _candidate(OTHER_CONDITION, percent=40, share=0.20),
        _candidate(4_400_003, percent=20, share=0.10),
    ]))

    assert len(actions) == 1
    assert len(refused) == 2
    assert all("такт" in row["reason"] for row in refused)


def test_the_surviving_audience_is_the_most_valuable_one():
    # Остаётся не первый по списку и не самый крупный сегмент, а тот, чей
    # ожидаемый прирост больше: доля сегмента на квадрат сдвига.
    actions, _ = _diff(_desired([
        _candidate(CONDITION, percent=10, share=0.30),
        _candidate(OTHER_CONDITION, percent=40, share=0.20),
    ]))
    assert actions[0]["key"] == str(OTHER_CONDITION)


def test_the_refusal_names_the_audience_that_won():
    _, refused = _diff(_desired([
        _candidate(CONDITION, percent=10, share=0.05),
        _candidate(OTHER_CONDITION, percent=40, share=0.20),
    ]))
    assert str(OTHER_CONDITION) in refused[0]["reason"]
    assert refused[0]["condition_id"] == CONDITION


def test_audiences_in_different_campaigns_do_not_crowd_each_other_out():
    # Правило — про кампанию, а не про прогон: сегменты разных кампаний
    # меряются раздельно и друг другу не мешают.
    desired = {CAMPAIGN: _desired()[CAMPAIGN],
               "222": _desired()[CAMPAIGN]}
    state = {CAMPAIGN: _state()[CAMPAIGN], "222": _state()[CAMPAIGN]}
    actions, refused = audience_mod.diff_audience(desired, state)
    assert len(actions) == 2 and refused == []


# ------------------------------------------- цена действия (шаг 3)


def test_a_known_share_pays_only_for_the_segment():
    """Шаг 3 задачи 23: доля известна — под ударом доля, а не кампания."""
    action = _action(_desired([_candidate(share=0.12, percent=30)]))
    assert action["exposure"] == exposure.bid_modifier_exposure(30, 0.12)
    assert action["exposure"]["share"] < 1.0


def test_an_unknown_share_puts_the_whole_object_at_risk():
    """Шаг 3 задачи 23: правило exposure.bid_modifier_exposure без исключений.

    Подстановка средней доли отличалась бы от «сегмент маленький» ничем, и
    гарантия недельного лимита перестала бы держаться: неизвестная доля — это
    весь объект под ударом, а не ноль.
    """
    action = _action(_desired([_candidate(share=None)]))
    assert action["exposure"]["share"] == 1.0
    assert "весь объект" in action["exposure"]["basis"]


def test_a_candidate_without_a_share_never_outranks_a_measured_one():
    # Незнание не аргумент: сегмент без доли не может оказаться «ценнее»
    # посчитанного — иначе он вытеснял бы измеренное каждым тактом.
    actions, _ = _diff(_desired([
        _candidate(CONDITION, share=None, percent=50),
        _candidate(OTHER_CONDITION, share=0.05, percent=10),
    ]))
    assert actions[0]["key"] == str(OTHER_CONDITION)


# ------------------------------------------- сегмент обязан работать


def test_an_audience_attached_to_nothing_is_refused():
    # Замер 26.08.2026: во всех четырёх кабинетах привязок условий к группам
    # ноль. Корректировка ставки по такому сегменту не тронет ни одного
    # показа — это действие, которое отчитается успехом и не сделает ничего.
    actions, refused = _diff(_desired([_candidate(attached=0)]))
    assert actions == []
    assert "не привязан" in refused[0]["reason"]


def test_an_unknown_attachment_is_refused_too():
    # «Не знаем, привязан ли» — не то же самое, что «привязан».
    actions, refused = _diff(_desired([_candidate(attached=None)]))
    assert actions == []
    assert refused[0]["reason"]


# ------------------------------------------- add поверх существующего


def test_an_existing_adjustment_is_not_added_a_second_time():
    # add поверх живого объекта создаёт ВТОРОЙ объект на тот же сегмент, а не
    # правит первый: правка существующего — это bidmodifier.set, и коэффициент
    # там мог поставить человек.
    actions, refused = _diff(state=_state([
        {"Id": 7, "Type": "RETARGETING_ADJUSTMENT", "key": str(CONDITION),
         "percent": 15}]))
    assert actions == []
    assert "уже стоит" in refused[0]["reason"]


def test_an_adjustment_on_another_segment_does_not_block_us():
    actions, refused = _diff(state=_state([
        {"Id": 7, "Type": "RETARGETING_ADJUSTMENT", "key": str(OTHER_CONDITION),
         "percent": 15}]))
    assert refused == [] and len(actions) == 1


def test_an_unusable_actual_record_refuses_instead_of_guessing():
    # Факт прочитан, но коэффициента в ответе не оказалось: прошлое состояние
    # сегмента неизвестно, и add поверх него создал бы второй объект.
    actions, refused = _diff(state=_state([
        {"Id": 7, "Type": "RETARGETING_ADJUSTMENT", "key": str(CONDITION),
         "percent": None, "unusable": True}]))
    assert actions == []
    assert refused[0]["reason"]


def test_unread_modifiers_are_not_an_empty_list():
    # Боевой читатель прогона (agent_e1._actual_modifiers) сегодня не
    # запрашивает RetargetingAdjustmentFieldNames вовсе. Прими рычаг молчание
    # за «корректировок нет» — он добавлял бы вторую поверх каждой живой.
    actions, refused = _diff(state={CAMPAIGN: {}})
    assert actions == []
    assert "не прочитан" in refused[0]["reason"]


def test_a_campaign_missing_from_the_cabinet_is_silent():
    actions, refused = audience_mod.diff_audience(_desired(), {})
    assert actions == [] and refused == []


# ------------------------------------------- форма адреса и сдвига


def test_a_non_numeric_condition_is_refused():
    # RetargetingConditionId — идентификатор, а не имя: строку API не примет,
    # и отказ уровня элемента приехал бы внутри успешного HTTP-ответа.
    actions, refused = _diff(_desired([_candidate(condition_id="Купившие")]))
    assert actions == []
    assert refused[0]["reason"]


def test_a_zero_shift_is_refused():
    # Нулевая корректировка — объект в кабинете, который ничего не меняет.
    actions, refused = _diff(_desired([_candidate(percent=0)]))
    assert actions == []
    assert refused[0]["reason"]


def test_a_shift_beyond_the_cap_is_refused_by_the_lever_itself():
    # Потолок — общий (guardrails.MODIFIER_CAP), своего числа у рычага нет.
    # Отказ ставится здесь, чтобы негодный кандидат не занял единственное
    # место кампании и не вылетел потом на рельсе.
    over = guardrails.MODIFIER_CAP + 1
    actions, refused = _diff(_desired([_candidate(percent=over)]))
    assert actions == []
    assert str(guardrails.MODIFIER_CAP) in refused[0]["reason"]


# ------------------------------------------- форма запроса


def test_the_request_form_matches_what_the_api_takes():
    """Форма подтверждена кабинетом, а не выведена по созвучию.

    probe_retargeting_lever.py 26.08.2026: bidmodifiers.add с
    RetargetingAdjustments принят на обоих уровнях (отказ 8800 «объект не
    найден» — уровня элемента, то есть форма верна). Чтение той же формы —
    sync/edu_direct_settings.py:886 и :927.
    """
    service, method, params = to_api_call(_action())
    assert (service, method) == ("bidmodifiers", "add")
    item = params["BidModifiers"][0]
    assert item["CampaignId"] == int(CAMPAIGN)
    assert item["RetargetingAdjustments"] == [
        {"RetargetingConditionId": CONDITION, "BidModifier": 130}]


def test_the_action_carries_its_segment_address_on_the_top_level():
    # По типу и ключу адресуются кулдаун после отката и счётчик попыток: без
    # них сегмент жил бы только внутри payload и вне этих механизмов.
    action = _action()
    assert action["direct_type"] == "RETARGETING_ADJUSTMENT"
    assert action["key"] == str(CONDITION)
    assert action["object_level"] == "campaign"
    assert action["object_id"] == CAMPAIGN


def test_the_idempotency_key_is_the_one_of_a_segment_adjustment():
    # Ключ считается ТЕМ ЖЕ способом, что у корректировки сегмента: одно и то
    # же изменение кабинета не имеет права приезжать под двумя ключами.
    assert _action()["idempotency_key"] == bidmod_idempotency_key(
        CAMPAIGN, "RETARGETING_ADJUSTMENT", str(CONDITION), 30)


# ------------------------------------------- класс, обещание, рельсы


def test_the_promise_is_the_segment_reallocation_model():
    exp = expectation.of(_action(), {})
    # 12 000 ₽/дн × 0.12 доли × 0.30 сдвига = 432 ₽/дн переносится;
    # 432 × 0.30 / 2 400 ₽ = 0.054 лида в день × 7 дней замера.
    assert exp["leads_delta"] == pytest.approx(0.38, abs=0.01)
    assert exp["rub_delta"] == 0.0
    assert exp["basis"].strip()


def test_the_adjustment_is_measured_not_a_bet():
    # История сегмента снята с ЭТОЙ кампании, а не перенесена с соседней:
    # класс 1, и платит он как измерение.
    assert tier.tier_of(_action()) == tier.TIER_MEASURED


def test_the_kind_is_allowed_to_be_applied():
    assert audience_mod.AUDIENCE_KIND in guardrails.ALLOWED_ACTION_KINDS
    assert audience_mod.AUDIENCE_KIND in lanes.LANE_OF_KIND


def test_the_action_passes_the_rail():
    ok, reason = guardrails.check_action(_action())
    assert ok, reason


def test_the_rail_keeps_its_own_cap_over_the_lever():
    # Рельса считает потолок сама, по payload.BidModifier: она обязана ловить
    # и то, что рычаг собрал мимо своей проверки.
    action = _action()
    action["payload"]["BidModifier"] = guardrails.MODIFIER_CAP + 5
    ok, reason = guardrails.check_action(action)
    assert not ok and "потолок" in reason


# ------------------------------------------- путь назад


def test_the_rollback_sets_the_added_adjustment_to_neutral():
    # Откат добавления ничего не удаляет: он ставит нейтраль (100), а не ноль
    # — ноль означал бы «ставка × 0», то есть удар сильнее исходного.
    from sync.agent.writer.rollback import rollback_payload

    action = {**_action(), "response": {"AddResults": [{"Id": 555}]}}
    service, method, params = rollback_payload(action)
    assert (service, method) == ("bidmodifiers", "set")
    assert params["BidModifiers"] == [{"Id": 555, "BidModifier": 100}]


def test_the_rollback_without_an_id_is_refused_instead_of_blind():
    from sync.agent.writer.rollback import rollback_payload

    assert rollback_payload(_action()) is None


def test_the_rollback_request_passes_the_return_rail():
    from sync.agent.writer.guardrails import check_rollback
    from sync.agent.writer.rollback import rollback_payload
    from sync.agent_e1_watchdog import rollback_guard_form

    action = {**_action(), "response": {"AddResults": [{"Id": 555}]}}
    service, method, params = rollback_payload(action)
    ok, reason = check_rollback(rollback_guard_form(action, service, method, params))
    assert ok, reason


def test_the_previous_state_is_empty_because_the_object_did_not_exist():
    assert _action()["previous_state"] == {}
