# -*- coding: utf-8 -*-
"""
tests/test_agent_writer_lanes.py — полосы действий вместо общего лимита прогона.

Данные — литералы в форме действия движка записи (action_kind + payload):
модуль ничего не читает и никуда не пишет, он отвечает на два вопроса —
в какой полосе действие и что этой полосе положено на её ступени.
"""

import pytest

from sync.agent import autonomy, config
from sync.agent.writer import guardrails, lanes, switch


# ------------------- пять тестов спеки (§Ф11, задача 4)


def test_every_allowed_kind_has_exactly_one_lane():
    for kind in guardrails.ALLOWED_ACTION_KINDS:
        lane = lanes.lane_of({"action_kind": kind, "payload": {}})
        assert lane in lanes.ALL_LANES, kind


def test_exploration_flag_wins_over_kind():
    action = {"action_kind": "budget.set", "payload": {"exploration": {"rub": 5000}}}
    assert lanes.lane_of(action) == lanes.LANE_EXPLORATION


def test_hygiene_pays_no_risk_but_is_capped_by_cut_share():
    p = lanes.policy_of(lanes.LANE_HYGIENE, step=1)
    assert p.risk_share == 0.0
    assert p.max_cut_share is not None and 0.0 < p.max_cut_share <= 0.10


def test_shadow_step_gives_no_budget_to_any_lane():
    for lane in lanes.ALL_LANES:
        assert lanes.policy_of(lane, step=0).risk_share == 0.0


def test_panel_keys_survive_the_move_to_lanes():
    assert "max_suspends_per_run" in config.SPEC          # стал политикой полосы 4
    assert "max_actions_per_run" not in config.SPEC       # такого ключа и не было
    assert config.resolve(overrides={"max_suspends_per_run": 1})["max_suspends_per_run"] == 1


# ------------------- карта видов действий


def test_seven_lanes_and_no_duplicates():
    assert len(lanes.ALL_LANES) == 7
    assert len(set(lanes.ALL_LANES)) == 7


def test_kinds_land_in_the_lane_the_plan_names():
    def lane(kind):
        return lanes.lane_of({"action_kind": kind, "payload": {}})

    assert lane("negative.add") == lanes.LANE_HYGIENE
    assert lane("placement.exclude") == lanes.LANE_HYGIENE
    assert lane("bidmodifier.add") == lanes.LANE_TUNING
    assert lane("bidmodifier.set") == lanes.LANE_TUNING
    assert lane("schedule.set") == lanes.LANE_TUNING
    assert lane("budget.set") == lanes.LANE_ALLOCATION
    assert lane("budget.set_daily") == lanes.LANE_ALLOCATION
    assert lane("tcpa.set") == lanes.LANE_ALLOCATION
    assert lane("campaign.suspend") == lanes.LANE_SUSPEND


def test_future_kinds_already_know_their_lane():
    # Ф14–Ф15 приносят рычаги; карта полос знает их заранее, чтобы новый вид
    # не появился без лимита и без цены.
    def lane(kind):
        return lanes.lane_of({"action_kind": kind, "payload": {}})

    assert lane("campaign.create") == lanes.LANE_LAUNCH
    assert lane("campaign.resume") == lanes.LANE_LAUNCH
    assert lane("negative.remove_added") == lanes.LANE_HYGIENE
    assert lane("goal.set") == lanes.LANE_ALLOCATION
    assert lane("strategy.set") == lanes.LANE_ALLOCATION
    assert lane("geo.set") == lanes.LANE_ALLOCATION
    assert lane("audience.add") == lanes.LANE_TUNING


def test_map_knows_more_kinds_than_the_allow_list_passes():
    # Карта идёт впереди allow-листа: вид входит в ALLOWED_ACTION_KINDS только
    # вместе со своим рычагом и тестом, а полосу знает заранее.
    assert "campaign.create" in lanes.LANE_OF_KIND
    assert "campaign.create" not in guardrails.ALLOWED_ACTION_KINDS


def test_unknown_kind_raises_instead_of_passing_for_free():
    # Вид без полосы не имеет ни лимита, ни цены. Молчаливое «пусть будет
    # предложением» пропустило бы его мимо всех ограничителей бесплатно.
    with pytest.raises(ValueError):
        lanes.lane_of({"action_kind": "budget.multiply_by_two", "payload": {}})


def test_recommendation_without_a_lever_goes_to_proposals():
    # Мастер кампаний и смысловые гипотезы: рычага записи нет, аппарат тот же.
    action = {"action_kind": "proposal.campaign_master", "payload": {}}
    assert lanes.lane_of(action) == lanes.LANE_PROPOSAL


# ------------------- политики полос


def test_only_four_lanes_pay_the_risk_share():
    paying = {lanes.LANE_TUNING, lanes.LANE_ALLOCATION,
              lanes.LANE_SUSPEND, lanes.LANE_LAUNCH}
    for step in (1, 2, 3):
        for lane in lanes.ALL_LANES:
            share = lanes.policy_of(lane, step=step).risk_share
            if lane in paying:
                assert share == autonomy.share_of(step), (lane, step)
            else:
                # Гигиена высвобождает деньги, разведка платит из кармана
                # explore_share, предложения не применяются.
                assert share == 0.0, (lane, step)


def test_measure_days_match_the_plan_table():
    days = {lane: lanes.policy_of(lane, step=1).measure_days for lane in lanes.ALL_LANES}
    assert days[lanes.LANE_HYGIENE] == 3
    assert days[lanes.LANE_TUNING] == 7
    assert days[lanes.LANE_ALLOCATION] == 14
    assert days[lanes.LANE_SUSPEND] == 14
    assert days[lanes.LANE_EXPLORATION] == 14
    assert days[lanes.LANE_LAUNCH] == 30


def test_shadow_step_applies_nothing_at_all():
    # Ноль риск-доли запирает только те полосы, которые риском платят.
    # Гигиена риска не платит — в тени её обязан держать счётчик объектов.
    for lane in lanes.ALL_LANES:
        assert lanes.policy_of(lane, step=0).max_objects_per_run == 0, lane
    assert lanes.policy_of(lanes.LANE_HYGIENE, step=0).max_cut_share == 0.0


def test_proposal_lane_never_applies_at_any_step():
    for step in (0, 1, 2, 3):
        assert lanes.policy_of(lanes.LANE_PROPOSAL, step=step).max_objects_per_run == 0


def test_suspend_cap_comes_from_the_panel():
    default = lanes.policy_of(lanes.LANE_SUSPEND, step=1)
    assert default.max_objects_per_run == switch.MAX_SUSPENDS_PER_RUN

    tuned = lanes.policy_of(lanes.LANE_SUSPEND, step=1,
                            config={"max_suspends_per_run": 2})
    assert tuned.max_objects_per_run == 2


def test_one_action_per_object_until_the_lever_is_proven():
    for step in (1, 2):
        assert lanes.policy_of(lanes.LANE_TUNING, step=step).max_actions_per_object == 1
        assert lanes.policy_of(lanes.LANE_ALLOCATION, step=step).max_actions_per_object == 1
    # На верхней ступени измеримость держат заповедник и замер такта, а не
    # искусственная редкость правок (задача 7, шаг 3).
    assert lanes.policy_of(lanes.LANE_TUNING, step=3).max_actions_per_object is None
    assert lanes.policy_of(lanes.LANE_ALLOCATION, step=3).max_actions_per_object is None


def test_hygiene_is_not_rationed_by_object():
    # Класс 0 вносится весь и сразу; его единственный ограничитель — рубли.
    p = lanes.policy_of(lanes.LANE_HYGIENE, step=1)
    assert p.max_actions_per_object is None
    assert p.max_objects_per_run is None


def test_unknown_lane_raises():
    with pytest.raises(ValueError):
        lanes.policy_of("budgets", step=1)


def test_unknown_step_raises():
    with pytest.raises(ValueError):
        lanes.policy_of(lanes.LANE_TUNING, step=4)
