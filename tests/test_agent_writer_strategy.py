# -*- coding: utf-8 -*-
"""
tests/test_agent_writer_strategy.py — рычаг смены стратегии (задача 22).

Стратегия — не настройка кампании, а способ, которым Директ принимает КАЖДОЕ
решение о показе. Перевод «клики → конверсии» на кампании, у которой конверсий
нет, — не «чуть хуже результат», а стратегия без сигнала: она будет учиться на
пустоте и тратить, пока кто-нибудь не заметит.

Обратный перевод «конверсии → клики» — не улучшение и не оптимизация, а
спасение: он законен ровно тогда, когда цель, на которой училась стратегия,
перестала приходить. Работающую конверсионную стратегию агент не имеет
основания разворачивать в клики.

Форма запроса не выдумывается. Справочник форм собран по тому, что читает из
кабинета sync/edu_direct_settings.py, а всё за его пределами — отказ: имя
подблока параметров выводится из типа стратегии, и ошибка в нём означает не
«поле проигнорировано», а отказ API целиком.
"""

import pytest

from sync.agent.writer import (expectation, guardrails, lanes, learning,
                               strategy as strategy_mod, tier)
from sync.agent.writer.apply import to_api_call

CAMPAIGN = "111"
LIVE_GOAL = 541_664_134
DEAD_GOAL = 593_523_067
M = 1_000_000


def _clicks_strategy():
    """Ручная стратегия: лимита внутри нет, деньги держит DailyBudget."""
    return {"Search": {"BiddingStrategyType": "HIGHEST_POSITION"},
            "Network": {"BiddingStrategyType": "SERVING_OFF"}}


def _cpa_strategy(goal_id=LIVE_GOAL):
    return {"Search": {"BiddingStrategyType": "AVERAGE_CPA",
                       "AverageCpa": {"AverageCpa": 2_400 * M,
                                      "WeeklySpendLimit": 80_000 * M,
                                      "BidCeiling": 300 * M,
                                      "PriorityGoals": [{"GoalId": goal_id}]}},
            "Network": {"BiddingStrategyType": "SERVING_OFF"}}


def _state(strategy=None, daily=True, **over):
    state = {"campaign_id": CAMPAIGN, "campaign_type": "TEXT_CAMPAIGN",
             "package_id": None,
             "strategy": _clicks_strategy() if strategy is None else strategy,
             "daily_budget": ({"Amount": 12_000 * M, "Mode": "STANDARD"}
                              if daily else None)}
    state.update(over)
    return state


def _to_cpa(reaches=400.0, **over):
    """Ход «клики → конверсии»: цель, её достижения и деньги стратегии."""
    move = {"strategy_type": "AVERAGE_CPA", "goal_ids": [LIVE_GOAL],
            "reaches": {LIVE_GOAL: reaches}, "window_days": 28,
            "target_cpa": 2_400.0, "weekly_limit": 80_000.0,
            "clicks_per_day": 120.0, "cr_current": 0.020, "cr_new": 0.026}
    move.update(over)
    return {CAMPAIGN: move}


def _to_clicks(reaches=0.0, **over):
    """Ход «конверсии → клики»: спасение кампании с умершей целью."""
    move = {"strategy_type": "HIGHEST_POSITION", "goal_ids": [LIVE_GOAL],
            "reaches": {LIVE_GOAL: reaches}, "window_days": 28,
            "clicks_per_day": 120.0, "cr_current": 0.020, "cr_new": 0.020}
    move.update(over)
    return {CAMPAIGN: move}


def _diff(desired=None, state=None):
    return strategy_mod.diff_strategy(desired or _to_cpa(),
                                      {CAMPAIGN: state or _state()})


# ------------------------------------------------- накопленная статистика


def test_a_switch_to_conversions_without_conversions_is_refused():
    # Шаг 1 задачи 22. Стратегия учится на достижениях цели; их нет — учиться
    # не на чем, и «оптимизация конверсий» становится тратой вслепую.
    actions, refused = _diff(_to_cpa(reaches=0))
    assert actions == []
    assert "не приходит" in refused[0]["reason"]


def test_a_switch_to_conversions_on_thin_statistics_is_refused():
    # Порог тот же, что у смены цели и у любого решения об объекте
    # (power.MIN_EXPECTED_PAYMENTS): две копии одного числа разъехались бы.
    from sync.agent.writer.goal import MIN_GOAL_REACHES

    actions, refused = _diff(_to_cpa(reaches=MIN_GOAL_REACHES - 1))
    assert actions == []
    assert "мало" in refused[0]["reason"]


def test_accumulated_statistics_produce_the_action():
    actions, refused = _diff()
    assert refused == []
    assert actions[0]["action_kind"] == strategy_mod.STRATEGY_KIND
    assert actions[0]["object_id"] == CAMPAIGN


# ------------------------------------------------- деньги не теряются при переходе


def test_a_conversion_strategy_without_a_weekly_limit_is_refused():
    # У ручной стратегии деньги держит DailyBudget кампании, у конверсионной —
    # WeeklySpendLimit ВНУТРИ стратегии. Переход без лимита оставил бы
    # кампанию без ограничения расхода вовсе.
    actions, refused = _diff(_to_cpa(weekly_limit=None))
    assert actions == []
    assert "лимит" in refused[0]["reason"]


def test_a_conversion_strategy_without_a_target_cpa_is_refused():
    actions, refused = _diff(_to_cpa(target_cpa=None))
    assert actions == []
    assert "цел" in refused[0]["reason"]


def test_a_switch_to_clicks_without_a_daily_budget_is_refused():
    # Обратный переход уносит недельный лимит вместе со стратегией: без
    # дневного бюджета кампания осталась бы без ограничения расхода.
    actions, refused = strategy_mod.diff_strategy(
        _to_clicks(), {CAMPAIGN: _state(strategy=_cpa_strategy(), daily=False)})
    assert actions == []
    assert "ограничен" in refused[0]["reason"]


# ------------------------------------------------- обратный переход — спасение, не выбор


def test_a_working_conversion_strategy_is_not_turned_back_to_clicks():
    actions, refused = strategy_mod.diff_strategy(
        _to_clicks(reaches=400.0), {CAMPAIGN: _state(strategy=_cpa_strategy())})
    assert actions == []
    assert "работает" in refused[0]["reason"]


def test_a_dead_goal_justifies_the_switch_back_to_clicks():
    actions, refused = strategy_mod.diff_strategy(
        _to_clicks(reaches=0.0), {CAMPAIGN: _state(strategy=_cpa_strategy())})
    assert refused == []
    search = actions[0]["payload"]["BiddingStrategy"]["Search"]
    assert search["BiddingStrategyType"] == "HIGHEST_POSITION"
    # Подблок прежней стратегии обязан уйти вместе с ней: оставленный
    # AverageCpa в ручной стратегии — противоречивое тело запроса.
    assert "AverageCpa" not in search


# ------------------------------------------------- справочник форм закрыт


def test_an_unknown_target_strategy_is_refused():
    actions, refused = _diff(_to_cpa(strategy_type="WB_MAXIMUM_CLICKS"))
    assert actions == []
    assert "справочник" in refused[0]["reason"]


def test_an_unknown_current_strategy_is_refused():
    # Не зная, чем кампания оптимизируется сейчас, нельзя сказать даже, в
    # какую сторону идёт переход — клики это или конверсии.
    unknown = {"Search": {"BiddingStrategyType": "WB_MAXIMUM_CONVERSION_RATE",
                          "WbMaximumConversionRate": {"WeeklySpendLimit": 80_000 * M}},
               "Network": {"BiddingStrategyType": "SERVING_OFF"}}
    actions, refused = _diff(state=_state(strategy=unknown))
    assert actions == []
    assert "справочник" in refused[0]["reason"]


def test_the_same_strategy_is_not_an_action():
    actions, refused = strategy_mod.diff_strategy(
        _to_cpa(), {CAMPAIGN: _state(strategy=_cpa_strategy())})
    assert actions == [] and refused == []


def test_a_campaign_in_a_package_strategy_is_refused():
    actions, refused = _diff(state=_state(package_id="777"))
    assert actions == []
    assert "пакет" in refused[0]["reason"]


def test_a_non_text_campaign_is_refused():
    actions, refused = _diff(state=_state(campaign_type="DYNAMIC_TEXT_CAMPAIGN"))
    assert actions == []
    assert refused[0]["reason"]


def test_a_campaign_missing_from_the_cabinet_is_silent():
    actions, refused = strategy_mod.diff_strategy(_to_cpa(), {})
    assert actions == [] and refused == []


# ------------------------------------------------- цена и класс


def test_switching_the_strategy_resets_learning():
    assert strategy_mod.STRATEGY_KIND in learning.RESETS_LEARNING
    assert learning.learning_impact(_diff()[0][0]) == "resets"


def test_the_lever_lives_in_the_allocation_lane():
    assert lanes.lane_of(_diff()[0][0]) == lanes.LANE_ALLOCATION


def test_the_whole_campaign_is_at_risk():
    assert _diff()[0][0]["exposure"]["share"] == 1.0


def test_the_switch_is_a_bet():
    # Под новой стратегией эта кампания не работала ни дня: любое число о ней
    # перенесено с соседнего объекта.
    assert tier.tier_of(_diff()[0][0]) == tier.TIER_BET


# ------------------------------------------------- форма запроса


def test_the_request_form_matches_what_the_api_takes():
    service, method, params = to_api_call(_diff()[0][0])
    assert (service, method) == ("campaigns", "update")
    campaign = params["Campaigns"][0]
    assert campaign["Id"] == int(CAMPAIGN)
    search = campaign["TextCampaign"]["BiddingStrategy"]["Search"]
    assert search["BiddingStrategyType"] == "AVERAGE_CPA"
    assert search["AverageCpa"]["AverageCpa"] == 2_400 * M
    assert search["AverageCpa"]["WeeklySpendLimit"] == 80_000 * M


def test_the_network_channel_travels_as_read():
    # Меняется канал поиска; сетевой уходит ровно таким, каким прочитан —
    # пересобери мы и его, настройки человека пропали бы молча.
    strategy = _diff()[0][0]["payload"]["BiddingStrategy"]
    assert strategy["Network"] == {"BiddingStrategyType": "SERVING_OFF"}


def test_the_goal_travels_into_the_new_strategy():
    # Конверсионная стратегия без цели не имеет смысла: учиться ей не на чем.
    search = _diff()[0][0]["payload"]["BiddingStrategy"]["Search"]
    assert search["AverageCpa"]["PriorityGoals"] == [{"GoalId": LIVE_GOAL}]


# ------------------------------------------------- путь назад


def test_the_previous_state_carries_both_the_type_and_the_parameters():
    # Шаг 3 задачи 22. Тип без параметров вернул бы кампанию в стратегию с
    # чужими деньгами; параметры без типа не вернули бы ничего.
    previous = _diff()[0][0]["previous_state"]
    assert previous["BiddingStrategyTypes"]["Search"] == "HIGHEST_POSITION"
    assert previous["BiddingStrategy"] == _clicks_strategy()


def test_the_rollback_returns_the_whole_previous_strategy():
    from sync.agent.writer.rollback import rollback_payload

    service, method, params = rollback_payload(_diff()[0][0])
    assert (service, method) == ("campaigns", "update")
    block = params["Campaigns"][0]["TextCampaign"]["BiddingStrategy"]
    assert block["Search"]["BiddingStrategyType"] == "HIGHEST_POSITION"


def test_the_rollback_request_passes_the_return_rail():
    from sync.agent.writer.guardrails import check_rollback
    from sync.agent.writer.rollback import rollback_payload
    from sync.agent_e1_watchdog import rollback_guard_form

    action = _diff()[0][0]
    service, method, params = rollback_payload(action)
    ok, reason = check_rollback(rollback_guard_form(action, service, method, params))
    assert ok, reason


# ------------------------------------------------- один такт — один блок


def test_strategy_and_budget_do_not_go_in_one_tick():
    """Шаг 2 задачи 22. Оба действия ЗАМЕНЯЮТ один и тот же блок кабинета
    (BiddingStrategy) и оба собраны из одного прочитанного состояния: второе
    молча вернуло бы то, что поставило первое, а наблюдение потом судило бы
    обоих по одному исходу.
    """
    from sync.agent import conflicts

    budget_action = {"action_kind": "budget.set", "object_level": "campaign",
                     "object_id": CAMPAIGN, "direct_type": "WEEKLY_SPEND_LIMIT",
                     "payload": {"CampaignId": int(CAMPAIGN),
                                 "BiddingStrategy": _clicks_strategy(),
                                 "WeeklySpendLimit": 90_000 * M},
                     "previous_state": {"WeeklySpendLimit": 80_000 * M}}
    kept, dropped = conflicts.resolve([_diff()[0][0], budget_action])

    assert len(kept) == 1
    assert [a["action_kind"] for a in dropped] == ["budget.set"]
    assert dropped[0]["conflict_reason"] == conflicts.SAME_BLOCK


def test_two_levers_on_different_blocks_still_go_together():
    # Правило узкое: оно про ОДИН блок, а не про «одну кампанию». Дневной
    # бюджет и расписание живут своими полями и друг друга не затирают.
    from sync.agent import conflicts

    daily = {"action_kind": "budget.set_daily", "object_level": "campaign",
             "object_id": CAMPAIGN, "direct_type": "DAILY_BUDGET",
             "payload": {"CampaignId": int(CAMPAIGN),
                         "DailyBudget": {"Amount": 14_000 * M}},
             "previous_state": {"DailyBudget": {"Amount": 12_000 * M}}}
    schedule = {"action_kind": "schedule.set", "object_level": "campaign",
                "object_id": CAMPAIGN, "direct_type": "TIME_TARGETING",
                "payload": {"CampaignId": int(CAMPAIGN),
                            "TimeTargeting": {"HolidaysSchedule": {}}}}
    kept, dropped = conflicts.resolve([daily, schedule])
    assert len(kept) == 2 and dropped == []


# ------------------------------------------------- обещание и ключ


def test_the_expectation_is_the_difference_of_conversion_rates():
    exp = expectation.of(_diff()[0][0], {})
    assert exp["rub_delta"] == 0.0
    assert exp["leads_delta"] == pytest.approx(10.08, abs=0.01)
    assert exp["measure_days"] == lanes.MEASURE_DAYS[lanes.LANE_ALLOCATION]
    assert "стратег" in exp["basis"]


def test_the_key_changes_with_the_target_strategy():
    to_cpa = _diff()[0][0]["idempotency_key"]
    back = strategy_mod.diff_strategy(
        _to_clicks(), {CAMPAIGN: _state(strategy=_cpa_strategy())})[0][0]
    assert to_cpa != back["idempotency_key"]


def test_the_kind_is_allowed_to_be_applied_and_rolled_back():
    assert strategy_mod.STRATEGY_KIND in guardrails.ALLOWED_ACTION_KINDS
    assert strategy_mod.STRATEGY_KIND in guardrails.ROLLBACK_ALLOWED_ACTION_KINDS


# ------------------------------------------------- рельса пути в кабинет


def test_the_action_passes_its_own_rail():
    ok, reason = guardrails.check_action(_diff()[0][0],
                                         cost_28d_by_campaign={CAMPAIGN: 336_000.0})
    assert ok, reason


def test_the_rail_refuses_a_body_whose_type_disagrees_with_itself():
    # Разойдись тип в поле действия и тип в блоке — в кабинет уедет одно, а в
    # журнал ляжет другое, и откат вернёт не туда.
    action = _diff()[0][0]
    action["payload"]["BiddingStrategyType"] = "HIGHEST_POSITION"
    ok, reason = guardrails.check_action(action)
    assert not ok and "не совпадает" in reason


def test_the_rail_refuses_a_conversion_strategy_without_a_limit():
    action = _diff()[0][0]
    del action["payload"]["BiddingStrategy"]["Search"]["AverageCpa"]["WeeklySpendLimit"]
    ok, reason = guardrails.check_action(action)
    assert not ok and "нечитаемы" in reason


def test_the_rail_catches_a_cpa_above_the_weekly_limit():
    # Рубли вместо микрорублей в лимите: цель конверсии оказывается дороже,
    # чем вся неделя. Такого решения не бывает — это сломанные единицы.
    action = _diff()[0][0]
    action["payload"]["BiddingStrategy"]["Search"]["AverageCpa"]["WeeklySpendLimit"] = 80_000
    ok, reason = guardrails.check_action(action)
    assert not ok and "единиц" in reason


def test_the_rail_checks_the_limit_against_the_independent_spend():
    # Лимит сам по себе читается, цель его не превышает — и всё же он в
    # четырнадцать раз меньше того, что кампания тратит по витрине. Число
    # берётся НЕ из тела действия: рельса, доверяющая тому, кого проверяет, —
    # не рельса.
    action = _diff()[0][0]
    action["payload"]["BiddingStrategy"]["Search"]["AverageCpa"]["WeeklySpendLimit"] = 5_000 * M
    ok, reason = guardrails.check_action(action, cost_28d_by_campaign={CAMPAIGN: 336_000.0})
    assert not ok and "коридор" in reason


def test_the_rail_refuses_leftovers_of_the_previous_strategy():
    # Ручная стратегия с подблоком AverageCpa — противоречивое тело: Директ
    # отвечает на такое отказом уровня элемента, то есть молча внутри 200.
    action = strategy_mod.diff_strategy(
        _to_clicks(), {CAMPAIGN: _state(strategy=_cpa_strategy())})[0][0]
    action["payload"]["BiddingStrategy"]["Search"]["AverageCpa"] = {"AverageCpa": 2_400 * M}
    ok, reason = guardrails.check_action(action)
    assert not ok and "прежней" in reason
