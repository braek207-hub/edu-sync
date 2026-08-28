# -*- coding: utf-8 -*-
"""
tests/test_agent_writer_goal.py — рычаг смены цели оптимизации (задача 21).

Цель оптимизации — то, ЧЕМУ стратегия учится. Ошибка здесь не «немного хуже
результат»: кампания на цели, события которой не приходят, слепнет целиком.
Так и вышло у LIME — цель 1900016999 сломалась 15.06, и смарт-кампании
крутились вслепую до самого аудита (память lime-direct-drr-audit-2026-08). В
EDU ровно та же заготовленная мина: ML-цели 593523067 и 593523237 заведены в
Метрике, но событий у них ноль — теги ЯТМ не вставлены (аудит 19.08.2026).

Поэтому первое, что делает рычаг, — проверяет, ЖИВА ЛИ ЦЕЛЬ. Не «есть ли она
в счётчике» (заведена она как раз есть), а приходят ли по ней достижения.

Второе: смена цели перезапускает обучение стратегии (справка Директа — тот же
класс, что смена стратегии и остановка кампании). Значит вид действия обязан
быть в writer/learning.RESETS_LEARNING, иначе кулдаун его не удержит и агент
будет мерить переобучение вместо эффекта.

Третье: форма запроса. Цель живёт внутри BiddingStrategy, и блок уходит в
API ЦЕЛИКОМ, как прочитан, с заменой одного поля: структура в Директе
заменяется, а не сливается по полям, и пересборка потеряла бы соседние
настройки (недельный лимит, цель CPA, потолок ставки).
"""

import pytest

from sync.agent import power
from sync.agent.writer import expectation, goal, guardrails, lanes, learning, tier
from sync.agent.writer.apply import to_api_call

CAMPAIGN = "111"
LIVE_GOAL = 541_664_134      # «CRM: Заказ создан» — событий много
DEAD_GOAL = 593_523_067      # ML-цель без тегов: заведена, событий ноль
CURRENT_GOAL = 213_000_001


def _strategy(goal_id=CURRENT_GOAL, *, value=None, priority=True):
    """Блок BiddingStrategy, как его отдаёт campaigns.get."""
    if priority:
        item = {"GoalId": goal_id}
        if value is not None:
            item["Value"] = value
        holder = {"PriorityGoals": [item]}
    else:
        holder = {"GoalId": goal_id}
    holder["AverageCpa"] = 1_500_000_000
    return {"Search": {"BiddingStrategyType": "AVERAGE_CPA",
                       "AverageCpa": holder},
            "Network": {"BiddingStrategyType": "NETWORK_DEFAULT"}}


def _state(**over):
    state = {"campaign_id": CAMPAIGN, "campaign_type": "TEXT_CAMPAIGN",
             "package_id": None, "strategy": _strategy()}
    state.update(over)
    return state


def _desired(goal_id=LIVE_GOAL, n=400.0, **over):
    """Ход рычага. n — достижения цели за окно; over перекрывает любое поле."""
    move = {"goal_ids": [goal_id], "reaches": {goal_id: n},
            "window_days": 28,
            "clicks_per_day": 120.0, "cr_current": 0.020, "cr_new": 0.026}
    move.update(over)
    return {CAMPAIGN: move}


def _diff(desired=None, state=None):
    return goal.diff_goal(desired or _desired(),
                          {CAMPAIGN: state or _state()})


# ------------------------------------------------- жива ли цель вообще


def test_a_goal_without_events_is_not_offered():
    # Шаг 1 задачи 21. Ноль достижений за окно — событие в кабинет не
    # приходит: цель заведена, а тега нет. Перевести на неё стратегию значит
    # ослепить кампанию — ровно то, что случилось у LIME.
    actions, refused = _diff(_desired(DEAD_GOAL, n=0))
    assert actions == []
    assert "не приходит" in refused[0]["reason"]
    assert str(DEAD_GOAL) in refused[0]["reason"]


def test_a_goal_too_thin_to_learn_on_is_refused():
    # Событие приходит, но редко. Порог не свой: столько достижений нужно,
    # чтобы решение на объекте вообще имело силу (power.MIN_EXPECTED_PAYMENTS).
    actions, refused = _diff(_desired(n=power.MIN_EXPECTED_PAYMENTS - 1))
    assert actions == []
    assert "мало" in refused[0]["reason"]


def test_a_goal_with_unknown_reach_count_is_refused():
    # Молчание о достижениях — не «их много». Неизвестность здесь означала бы
    # перевод стратегии на цель, о которой не известно ничего.
    actions, refused = _diff(_desired(n=None))
    assert actions == [] and refused


def test_a_live_goal_produces_the_action():
    actions, refused = _diff()
    assert refused == []
    assert len(actions) == 1
    assert actions[0]["action_kind"] == goal.GOAL_KIND
    assert actions[0]["object_id"] == CAMPAIGN


# ------------------------------------------------- обучение и кулдаун


def test_switching_the_goal_resets_learning():
    # Шаг 2. Справка Директа относит корректировку целевых действий к тому же
    # классу, что смену стратегии: обучение начинается заново.
    assert goal.GOAL_KIND in learning.RESETS_LEARNING
    assert learning.learning_impact(_diff()[0][0]) == "resets"


def test_the_lever_lives_in_the_allocation_lane():
    # Полоса 3: цель меняет, КУДА кампания тратит те же деньги.
    assert lanes.lane_of(_diff()[0][0]) == lanes.LANE_ALLOCATION


def test_the_whole_campaign_is_at_risk():
    # Под ударом не доля сегмента: стратегия переучивается целиком, и все
    # деньги кампании до конца замера идут по новому правилу.
    assert _diff()[0][0]["exposure"]["share"] == 1.0


def test_a_new_goal_is_a_bet_not_a_measurement():
    # Истории по новой цели у ЭТОЙ кампании нет по построению: число, из
    # которого посчитано обещание, снято с соседней. Класс 1 означал бы
    # «измерено здесь», и цена уверенности была бы занижена.
    assert tier.tier_of(_diff()[0][0]) == tier.TIER_BET


# ------------------------------------------------- форма запроса к API


def test_the_request_form_matches_what_the_api_takes():
    # Шаг 3. Цель живёт внутри BiddingStrategy; блок уходит целиком, как
    # прочитан, — структура в API заменяется, а не сливается по полям.
    service, method, params = to_api_call(_diff()[0][0])
    assert (service, method) == ("campaigns", "update")
    campaign = params["Campaigns"][0]
    assert campaign["Id"] == int(CAMPAIGN)
    strategy = campaign["TextCampaign"]["BiddingStrategy"]
    assert goal.read_goal_ids(strategy) == [LIVE_GOAL]


def test_the_neighbouring_strategy_settings_survive():
    # Пересборка блока потеряла бы цель CPA и потолок ставки, которые ставил
    # человек: в кабинет уходит ПРОЧИТАННЫЙ блок с заменой одного поля.
    action = _diff()[0][0]
    holder = action["payload"]["BiddingStrategy"]["Search"]["AverageCpa"]
    assert holder["AverageCpa"] == 1_500_000_000
    assert action["payload"]["BiddingStrategy"]["Network"] == {
        "BiddingStrategyType": "NETWORK_DEFAULT"}


def test_a_single_goal_holder_is_written_in_place():
    # У стратегий вида MaximumConversionRate цель лежит одиночным GoalId, а не
    # списком. Пишем в тот носитель, который прочитан: подмена формы — отказ
    # API целиком, а не «поле проигнорировано».
    actions, _ = _diff(state=_state(strategy=_strategy(priority=False)))
    strategy = actions[0]["payload"]["BiddingStrategy"]
    assert strategy["Search"]["AverageCpa"]["GoalId"] == LIVE_GOAL
    assert "PriorityGoals" not in strategy["Search"]["AverageCpa"]


def test_the_previous_goals_travel_for_the_rollback():
    action = _diff()[0][0]
    assert action["previous_state"]["GoalIds"] == [CURRENT_GOAL]
    assert goal.read_goal_ids(action["previous_state"]["BiddingStrategy"]) == [CURRENT_GOAL]


# ------------------------------------------------- когда рычаг молчит


def test_a_goal_already_set_is_not_an_action():
    actions, refused = _diff(_desired(CURRENT_GOAL))
    assert actions == [] and refused == []


def test_a_campaign_in_a_package_strategy_is_refused():
    actions, refused = _diff(state=_state(package_id="777"))
    assert actions == []
    assert "пакет" in refused[0]["reason"]


def test_a_strategy_without_a_goal_holder_is_refused():
    # Ручной стратегии цель оптимизации не назначается вовсе: писать её
    # некуда, и «поставить» значило бы создать поле, которого у объекта нет.
    manual = {"Search": {"BiddingStrategyType": "HIGHEST_POSITION"}}
    actions, refused = _diff(state=_state(strategy=manual))
    assert actions == []
    assert "носител" in refused[0]["reason"]


def test_a_goal_carrying_a_conversion_value_is_refused():
    # У стратегий с оплатой за конверсию цель несёт ЦЕНУ конверсии. Перенести
    # её на другую цель нельзя: ценность нового события неизвестна, а взять
    # прежнюю значило бы объявить их равными по деньгам.
    actions, refused = _diff(state=_state(strategy=_strategy(value=1_500_000_000)))
    assert actions == []
    assert "ценност" in refused[0]["reason"]


def test_a_campaign_missing_from_the_cabinet_is_silent():
    actions, refused = goal.diff_goal(_desired(), {})
    assert actions == [] and refused == []


# ------------------------------------------------- обещание


def test_the_expectation_is_the_difference_of_conversion_rates():
    # Расход не трогаем — обещание чисто по лидам: те же клики, другая доля
    # заявок. 120 кликов/дн × 14 дн × (2.6% − 2.0%) = +10.08 лида.
    action = _diff()[0][0]
    exp = expectation.of(action, {})
    assert exp["rub_delta"] == 0.0
    assert exp["leads_delta"] == pytest.approx(10.08, abs=0.01)
    assert exp["measure_days"] == lanes.MEASURE_DAYS[lanes.LANE_ALLOCATION]


def test_without_both_conversion_rates_nothing_is_promised():
    # Курс «клики → лиды» не выдумывается: без конверсии новой цели обещание
    # было бы прогнозом из воздуха, а петля обучения зачла бы его сбывшимся.
    actions, _ = _diff(_desired(cr_new=None))
    assert (actions[0]["payload"] or {}).get("expected_leads_delta") is None


# ------------------------------------------------- идемпотентность


def test_the_key_does_not_depend_on_the_order_of_goals():
    one = _diff(_desired(goal_ids=[LIVE_GOAL, 999], reaches={LIVE_GOAL: 400.0, 999: 400.0}))
    two = _diff(_desired(goal_ids=[999, LIVE_GOAL], reaches={999: 400.0, LIVE_GOAL: 400.0}))
    assert one[0][0]["idempotency_key"] == two[0][0]["idempotency_key"]


def test_the_kind_is_allowed_to_be_applied_and_rolled_back():
    assert goal.GOAL_KIND in guardrails.ALLOWED_ACTION_KINDS
    assert goal.GOAL_KIND in guardrails.ROLLBACK_ALLOWED_ACTION_KINDS


# ------------------------------------------------- путь назад


def test_the_rollback_returns_the_whole_previous_strategy():
    # Откат цели — не «поставить прежний GoalId»: назад едет прочитанный блок
    # целиком, иначе возврат стёр бы соседние настройки человека.
    from sync.agent.writer.rollback import rollback_payload

    service, method, params = rollback_payload(_diff()[0][0])
    assert (service, method) == ("campaigns", "update")
    strategy = params["Campaigns"][0]["TextCampaign"]["BiddingStrategy"]
    assert goal.read_goal_ids(strategy) == [CURRENT_GOAL]
    assert strategy["Search"]["AverageCpa"]["AverageCpa"] == 1_500_000_000


def test_the_cpa_target_rollback_is_built_too():
    """Дефект, найденный задачей 21: tcpa.set стоял в allow-листе возврата с
    самой Э3.5, а ветки в rollback_payload не имел — откат цели CPA не
    строился вовсе, и сторож хоронил его пометкой permanent.
    """
    from sync.agent.writer.rollback import rollback_payload

    action = {"action_kind": "tcpa.set",
              "payload": {"CampaignId": 111, "BiddingStrategy": _strategy()},
              "previous_state": {"BiddingStrategy": _strategy(),
                                 "TargetCpa": 1_500_000_000}}
    service, method, params = rollback_payload(action)
    assert (service, method) == ("campaigns", "update")
    assert params["Campaigns"][0]["TextCampaign"]["BiddingStrategy"]


def test_the_rollback_request_passes_the_return_rail():
    # Рельса возврата судит по СОДЕРЖИМОМУ запроса: назад едет блок
    # BiddingStrategy, и это тот же возврат, что у бюджета.
    from sync.agent.writer.guardrails import check_rollback
    from sync.agent.writer.rollback import rollback_payload
    from sync.agent_e1_watchdog import rollback_guard_form

    action = _diff()[0][0]
    service, method, params = rollback_payload(action)
    ok, reason = check_rollback(rollback_guard_form(action, service, method, params))
    assert ok, reason


def test_a_single_holder_refuses_a_list_of_goals():
    # Носитель один, целей несколько: подмена формы — отказ API целиком.
    desired = _desired(goal_ids=[LIVE_GOAL, 999],
                       reaches={LIVE_GOAL: 400.0, 999: 400.0})
    actions, refused = _diff(desired, _state(strategy=_strategy(priority=False)))
    assert actions == []
    assert "носител" in refused[0]["reason"]


# ------------------------------------------------- формы, в которых Директ отдаёт цели


def test_the_placeholder_goal_is_not_mistaken_for_a_goal():
    # GoalId 13 у Директа — не цель, а признак «цели заданы на уровне
    # кампании». Прими его за текущую цель — и рычаг записал бы настоящую
    # цель в поле-признак, не тронув того, чем кампания на самом деле
    # оптимизируется. Число прочитано у читателя кабинета
    # (edu_direct_settings._goal_ids_from_block), где отбрасывается так же.
    strategy = _strategy(goal_id=goal.PLACEHOLDER_GOAL_ID, priority=False)
    assert goal.read_goal_ids(strategy) == []
    actions, refused = _diff(state=_state(strategy=strategy))
    assert actions == []
    assert "13" in refused[0]["reason"]


def test_goals_wrapped_in_items_are_read():
    # Директ отдаёт PriorityGoals то массивом, то обёрткой Items, а элементы —
    # то словарями, то голыми числами (edu_direct_settings._as_list). Форма
    # уже, чем у читателя кабинета, означала бы отказ с неверной причиной:
    # «носителя нет» там, где цели есть.
    wrapped = _strategy()
    wrapped["Search"]["AverageCpa"]["PriorityGoals"] = {
        "Items": [{"GoalId": CURRENT_GOAL}]}
    assert goal.read_goal_ids(wrapped) == [CURRENT_GOAL]

    bare = _strategy()
    bare["Search"]["AverageCpa"]["PriorityGoals"] = [CURRENT_GOAL]
    assert goal.read_goal_ids(bare) == [CURRENT_GOAL]
