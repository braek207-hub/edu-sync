# -*- coding: utf-8 -*-
"""
tests/test_agent_writer_budget.py — тесты рычага бюджетов (Э3.3):
план сдвигов, чтение/замена лимита, diff, рельсы, путь возврата.
"""

import copy

from sync.agent.writer.apply import to_api_call
from sync.agent.writer.budget import (
    apply_cooldown,
    NO_LIMIT_REASON,
    NOT_APPLICABLE_UP_REASON,
    PACKAGE_REASON,
    TWO_CHANNEL_REASON,
    desired_weekly_micros,
    diff_budget,
    plan_budget_moves,
    read_weekly_limit,
    strategy_with_limit,
)
from sync.agent.writer.guardrails import check_action, check_rollback
from sync.agent.writer.rollback import rollback_payload
from sync.agent_e1_watchdog import rollback_guard_form

M = 1_000_000


def _target_row(target, current, rel_error=0.1):
    return {"setting_kind": "budget_target", "setting_key": "target_28d",
            "value": target, "raw_value": current, "support_n": 100,
            "rel_error": rel_error}


def _roi_row(roi, rel_error=0.1):
    return {"setting_kind": "budget_target", "setting_key": "roi_vs_lambda",
            "value": roi, "raw_value": 1.0, "support_n": 100,
            "rel_error": rel_error}


def _strategy(weekly_micros, channel="Search"):
    """Блок BiddingStrategy формы campaigns.get: лимит в одном канале."""
    other = "Network" if channel == "Search" else "Search"
    return {
        channel: {
            "BiddingStrategyType": "AVERAGE_CPA",
            "AverageCpa": {"AverageCpa": 3000 * M, "GoalId": 42,
                           "WeeklySpendLimit": weekly_micros},
        },
        other: {"BiddingStrategyType": "SERVING_OFF"},
    }


# ------------------------------------------------------------- конверсия


def test_weekly_micros_strips_vat_and_weeks():
    # 480 000 ₽ с НДС за 28 дней → 120 000/нед с НДС → 100 000/нед без НДС.
    assert desired_weekly_micros(480_000.0) == 100_000 * M


def test_weekly_micros_rounds_to_whole_rubles():
    micros = desired_weekly_micros(100_000.0)
    assert micros % M == 0


# ----------------------------------------------------------------- план


def test_plan_takes_confident_shift():
    plan = plan_budget_moves({"1": [_target_row(150_000, 100_000, rel_error=0.05),
                                    _roi_row(1.5, rel_error=0.05)]})
    assert "1" in plan["desired"]
    assert plan["desired"]["1"]["ratio"] == 1.5


def test_plan_rejects_low_confidence():
    # Ошибка решения такого размера не даёт p_sign>=0.90 для сдвига ×1.2.
    plan = plan_budget_moves({"1": [_target_row(120_000, 100_000, rel_error=0.9),
                                    _roi_row(1.2, rel_error=0.9)]})
    assert not plan["desired"]
    assert len(plan["low_confidence"]) == 1


def test_plan_skips_small_shift():
    plan = plan_budget_moves({"1": [_target_row(104_000, 100_000, rel_error=0.01)]})
    assert not plan["desired"]
    assert plan["small_shift"] == 1


def test_plan_counts_unknown_confidence():
    plan = plan_budget_moves({"1": [_target_row(150_000, 100_000, rel_error=None),
                                    _roi_row(1.5, rel_error=None)]})
    # Нет rel_error — уверенность неизвестна; для бюджета это НЕ допуск:
    # сдвиг без ошибки решения не планируется, но и не теряется молча.
    assert plan["confidence_unknown"] == 1
    assert "1" in plan["desired"]


# ------------------------------------------------------- чтение лимита


def test_read_limit_single_channel():
    channel, micros, reason = read_weekly_limit(_strategy(200_000 * M))
    assert (channel, micros, reason) == ("Search", 200_000 * M, "")


def test_read_limit_two_channels_refused():
    strategy = _strategy(200_000 * M)
    strategy["Network"] = {"BiddingStrategyType": "WB_MAXIMUM_CONVERSION_RATE",
                           "WbMaximumConversionRate": {"WeeklySpendLimit": 50_000 * M}}
    channel, _, reason = read_weekly_limit(strategy)
    assert channel is None
    assert reason == TWO_CHANNEL_REASON


def test_read_limit_absent():
    strategy = {"Search": {"BiddingStrategyType": "HIGHEST_POSITION"},
                "Network": {"BiddingStrategyType": "SERVING_OFF"}}
    channel, _, reason = read_weekly_limit(strategy)
    assert channel is None
    assert reason == NO_LIMIT_REASON


def test_strategy_with_limit_preserves_siblings_and_source():
    strategy = _strategy(200_000 * M)
    before = copy.deepcopy(strategy)
    out = strategy_with_limit(strategy, "Search", 90_000 * M)
    assert out["Search"]["AverageCpa"]["WeeklySpendLimit"] == 90_000 * M
    # Соседние поля стратегии не тронуты, исходный блок не мутирован.
    assert out["Search"]["AverageCpa"]["GoalId"] == 42
    assert strategy == before


# ------------------------------------------------------------------ diff


def _move(target, current, leads_delta=None, write_step=None):
    move = {"target_28d": target, "cost_28d": current,
            "ratio": round(target / current, 4), "p_sign": 0.99}
    if leads_delta is not None:
        move["expected_leads_delta"] = leads_delta
    if write_step is not None:
        move["write_step"] = write_step
    return move


def _state(weekly_micros=None, daily_micros=None, package_id=None,
           campaign_type="TEXT_CAMPAIGN"):
    return {
        "campaign_type": campaign_type,
        "strategy": _strategy(weekly_micros) if weekly_micros else {
            "Search": {"BiddingStrategyType": "HIGHEST_POSITION"},
            "Network": {"BiddingStrategyType": "SERVING_OFF"}},
        "daily_budget": ({"Amount": daily_micros, "Mode": "STANDARD"}
                         if daily_micros else None),
        "package_id": package_id,
    }


def test_down_move_builds_action_with_previous_state():
    # Расход 480 000/28д с НДС (100 000/нед без НДС), лимит висит на 5 млн.
    # Цель ×0.5 дожимается капом записи до −20% от РАСХОДА (80 000/нед):
    # лимит 5 млн — декорация, менять «бюджет» кампании значит менять её
    # расход, и правило спеки ±20%/14 дней меряется от него.
    actions, refused = diff_budget(
        {"1": _move(240_000, 480_000)},
        {"1": _state(weekly_micros=5_000_000 * M)},
        {"1": 100_000.0})
    assert not refused
    assert len(actions) == 1
    a = actions[0]
    assert a["action_kind"] == "budget.set"
    assert a["payload"]["WeeklySpendLimit"] == 80_000 * M
    assert a["previous_state"]["WeeklySpendLimit"] == 5_000_000 * M
    # Блок previous — целиком, для отката.
    assert "Search" in a["previous_state"]["BiddingStrategy"]


def test_up_move_on_loose_limit_refused():
    actions, refused = diff_budget(
        {"1": _move(720_000, 480_000)},
        {"1": _state(weekly_micros=5_000_000 * M)},
        {"1": 100_000.0})
    assert not actions
    assert refused[0]["reason"] == NOT_APPLICABLE_UP_REASON.format(share=0.9)


def test_up_move_on_binding_limit_builds_action():
    # Лимит 100 000/нед, расход 100 000/нед — упирается; цель ×1.5
    # дожимается капом записи до +20% от расхода.
    actions, refused = diff_budget(
        {"1": _move(720_000, 480_000)},
        {"1": _state(weekly_micros=100_000 * M)},
        {"1": 100_000.0})
    assert not refused
    assert actions[0]["payload"]["WeeklySpendLimit"] == 120_000 * M


def test_already_set_produces_nothing():
    # Цель 50к/нед дожата капом записи до −20 % от расхода 100к → 80к;
    # «уже стоит» проверяется о значении ПОСЛЕ капа — том, что реально
    # поехало бы в кабинет.
    actions, refused = diff_budget(
        {"1": _move(240_000, 480_000)},
        {"1": _state(weekly_micros=80_000 * M)},   # уже стоит целевой
        {"1": 100_000.0})
    assert not actions and not refused


def test_package_strategy_refused():
    actions, refused = diff_budget(
        {"1": _move(240_000, 480_000)},
        {"1": _state(weekly_micros=100_000 * M, package_id=708062738)},
        {"1": 100_000.0})
    assert not actions
    assert refused[0]["reason"] == PACKAGE_REASON.format(strategy_id=708062738)


def test_daily_budget_branch_for_manual_strategy():
    # HIGHEST_POSITION: лимита в стратегии нет, есть DailyBudget 30 000 ₽.
    actions, refused = diff_budget(
        {"1": _move(240_000, 480_000)},
        {"1": _state(daily_micros=30_000 * M)},
        {"1": 100_000.0})
    assert not refused
    a = actions[0]
    assert a["action_kind"] == "budget.set_daily"
    # Цель 50 000/нед → 7143 ₽/день, но кап записи −20% от дневного
    # расхода (100 000/нед → 14 286/день): пол 11 429 ₽, округление до рубля.
    assert a["payload"]["DailyBudget"]["Amount"] == 11_429 * M
    assert a["payload"]["DailyBudget"]["Mode"] == "STANDARD"
    assert a["previous_state"]["DailyBudget"]["Amount"] == 30_000 * M


def test_no_limit_no_daily_refused():
    actions, refused = diff_budget(
        {"1": _move(240_000, 480_000)},
        {"1": _state()},
        {"1": 100_000.0})
    assert not actions
    assert refused[0]["reason"] == NO_LIMIT_REASON


def test_unknown_campaign_type_refused():
    actions, refused = diff_budget(
        {"1": _move(240_000, 480_000)},
        {"1": _state(weekly_micros=100_000 * M, campaign_type="UNIFIED_CAMPAIGN")},
        {"1": 100_000.0})
    assert not actions
    assert len(refused) == 1


def test_missing_campaign_neither_action_nor_refusal():
    actions, refused = diff_budget({"1": _move(240_000, 480_000)}, {}, {})
    assert not actions and not refused


# ------------------------------------------------------------ API-формы


def _sample_action():
    actions, _ = diff_budget(
        {"1": _move(240_000, 480_000)},
        {"1": _state(weekly_micros=5_000_000 * M)},
        {"1": 100_000.0})
    return actions[0]


def test_to_api_call_sends_strategy_only():
    service, method, params = to_api_call(_sample_action())
    assert (service, method) == ("campaigns", "update")
    campaign = params["Campaigns"][0]
    assert campaign["Id"] == 1
    assert "BiddingStrategy" in campaign["TextCampaign"]
    # Служебные поля рельсы в API не уезжают.
    assert "Cost28dVat" not in str(params)


def test_to_api_call_daily():
    actions, _ = diff_budget(
        {"1": _move(240_000, 480_000)},
        {"1": _state(daily_micros=30_000 * M)},
        {"1": 100_000.0})
    service, method, params = to_api_call(actions[0])
    assert (service, method) == ("campaigns", "update")
    # Сырой дневной таргет 50к/7 ≈ 7 143 дожат капом до −20 % от
    # дневного расхода 100к/7 ≈ 14 286 → 11 429.
    assert params["Campaigns"][0]["DailyBudget"]["Amount"] == 11_429 * M


# ---------------------------------------------------------------- рельсы


def test_check_action_passes_valid_budget():
    ok, reason = check_action(_sample_action())
    assert ok, reason


def test_check_action_catches_unit_break():
    action = _sample_action()
    # Слом конверсии: недельный лимит посчитан как дневной ×7.
    action["payload"]["WeeklySpendLimit"] *= 7
    ok, reason = check_action(action)
    assert not ok
    assert "коридора" in reason


def test_check_action_requires_cost_field():
    action = _sample_action()
    del action["payload"]["Cost28dVat"]
    ok, _ = check_action(action)
    assert not ok


# ------------------------------------------------------------------ откат


def test_rollback_restores_whole_strategy():
    request = rollback_payload(_sample_action())
    assert request is not None
    service, method, params = request
    assert (service, method) == ("campaigns", "update")
    strategy = params["Campaigns"][0]["TextCampaign"]["BiddingStrategy"]
    assert strategy["Search"]["AverageCpa"]["WeeklySpendLimit"] == 5_000_000 * M


def test_rollback_without_previous_state_is_none():
    action = _sample_action()
    action["previous_state"] = {}
    assert rollback_payload(action) is None


def test_rollback_daily():
    actions, _ = diff_budget(
        {"1": _move(240_000, 480_000)},
        {"1": _state(daily_micros=30_000 * M)},
        {"1": 100_000.0})
    service, method, params = rollback_payload(actions[0])
    assert (service, method) == ("campaigns", "update")
    assert params["Campaigns"][0]["DailyBudget"]["Amount"] == 30_000 * M


def test_rollback_passes_return_guard():
    action = _sample_action()
    service, method, params = rollback_payload(action)
    form = rollback_guard_form(action, service, method, params)
    # Вид выведен из СОДЕРЖИМОГО запроса, не из журнала.
    assert form["action_kind"] == "budget.set"
    ok, reason = check_rollback(form)
    assert ok, reason


def test_guard_form_distinguishes_schedule_from_budget():
    form = rollback_guard_form(
        {"object_id": "1"}, "campaigns", "update",
        {"Campaigns": [{"Id": 1, "TimeTargeting": {"Schedule": {"Items": []}}}]})
    assert form["action_kind"] == "schedule.set"


# --------------------------- гейт уверенности — по экономике, не по шагу


def test_plan_gates_on_economic_edge_not_step_ratio():
    # Шаг ×1.5 при честной ошибке 0.1 давал z = ln(1.5)/0.1 ≈ 4 — «уверенно»,
    # даже когда экономическое преимущество (value против λ·marginal) на
    # грани шума. Гейт обязан мерить преимущество, а не размер шага.
    plan = plan_budget_moves({"1": [
        _target_row(150_000, 100_000, rel_error=0.1),
        _roi_row(1.05, rel_error=0.1),
    ]})
    assert plan["desired"] == {}
    assert plan["low_confidence"][0]["campaign_id"] == "1"


def test_plan_confident_edge_passes_and_carries_roi():
    plan = plan_budget_moves({"1": [
        _target_row(150_000, 100_000, rel_error=0.05),
        _roi_row(1.5, rel_error=0.05),
    ]})
    assert plan["desired"]["1"]["ratio"] == 1.5
    assert plan["desired"]["1"]["roi_vs_lambda"] == 1.5


def test_plan_without_roi_row_is_unknown_confidence():
    # Строки старого формата (до строки roi_vs_lambda) не несут экономического
    # отношения — уверенность по ним неизвестна, и применять их нельзя:
    # свежий прогон Э0 допишет отношение, ждать его — дешевле ложного сдвига.
    plan = plan_budget_moves({"1": [_target_row(150_000, 100_000,
                                                rel_error=0.05)]})
    assert plan["desired"] == {}
    assert plan["confidence_unknown"] == 1


# --------------------------- кулдаун бюджета: правило ±20% за 14 дней


def test_apply_cooldown_removes_recently_touched_campaigns():
    desired = {"1": {"target_28d": 1.0}, "2": {"target_28d": 2.0}}
    kept, cooled = apply_cooldown(desired, {"1", "9"})
    assert set(kept) == {"2"}
    assert cooled[0]["campaign_id"] == "1"
    assert "14" in cooled[0]["reason"]


def test_apply_cooldown_without_touched_is_passthrough():
    desired = {"1": {"target_28d": 1.0}}
    kept, cooled = apply_cooldown(desired, set())
    assert kept == desired and cooled == []


# --------------------------------------------------- ожидание солвера


def _expectation_row(leads_delta):
    return {"setting_kind": "budget_target", "setting_key": "expected_leads_delta",
            "value": leads_delta, "raw_value": leads_delta * 900.0,
            "support_n": 100, "rel_error": 0.1}


def test_plan_takes_expected_leads_delta_from_the_solver():
    # Число приходит готовым: формула кривой живёт в солвере, писатель её не
    # повторяет — иначе двух копий модели не избежать.
    plan = plan_budget_moves({"1": [_target_row(150_000, 100_000, rel_error=0.05),
                                    _roi_row(1.5, rel_error=0.05),
                                    _expectation_row(22.47)]})
    assert plan["desired"]["1"]["expected_leads_delta"] == 22.47


def test_plan_without_expectation_row_has_none():
    # Строки ожидания нет — ожидания нет вовсе: ноль петля обучения
    # прочитала бы как прогноз «эффекта не будет».
    plan = plan_budget_moves({"1": [_target_row(150_000, 100_000, rel_error=0.05),
                                    _roi_row(1.5, rel_error=0.05)]})
    assert "expected_leads_delta" not in plan["desired"]["1"]


def test_budget_action_carries_expectation():
    """Ожидаемая дельта лидов едет в payload действия.

    Солвер её считает, а журнал терял: сравнить прогноз с исходом было
    невозможно, и калибровка модели держалась на вере в модель.
    """
    actions, _ = diff_budget(
        {"1": _move(720_000, 480_000, leads_delta=6.0)},
        {"1": _state(weekly_micros=100_000 * M)},
        {"1": 100_000.0})
    assert actions[0]["payload"]["expected_leads_delta"] == 6.0


def test_daily_budget_action_carries_expectation():
    # Второй сборщик действия — ручная стратегия: ожидание обязано ехать и
    # там, иначе половина журнала осталась бы без прогноза.
    actions, _ = diff_budget(
        {"1": _move(240_000, 480_000, leads_delta=-4.0)},
        {"1": _state(daily_micros=20_000 * M)},
        {"1": 100_000.0})
    assert actions[0]["payload"]["expected_leads_delta"] == -4.0


# ---------------------------------------------- адресный кап записи (×2)


def _write_step_row(step):
    return {"setting_kind": "budget_target", "setting_key": "write_step",
            "value": step, "raw_value": 2.0, "support_n": 100,
            "rel_error": 0.05}


def test_plan_carries_the_addressed_write_step():
    plan = plan_budget_moves({"1": [_target_row(200_000, 100_000, rel_error=0.05),
                                    _roi_row(1.6, rel_error=0.05),
                                    _write_step_row(1.0)]})
    assert plan["desired"]["1"]["write_step"] == 1.0


def test_plan_without_write_step_row_keeps_the_default():
    plan = plan_budget_moves({"1": [_target_row(150_000, 100_000, rel_error=0.05),
                                    _roi_row(1.5, rel_error=0.05)]})
    assert "write_step" not in plan["desired"]["1"]


def test_double_step_reaches_the_account():
    # Без параметризации кап записи ±20 % убивал адресный шаг ×2 на последнем
    # метре: солвер назначал 200 000 ₽/нед, а в кабинет уезжало 120 000.
    # Тесты этого не ловили — они не доходили до писателя.
    actions, refused = diff_budget(
        {"1": _move(960_000, 480_000, write_step=1.0)},
        {"1": _state(weekly_micros=100_000 * M)},
        {"1": 100_000.0})
    assert not refused
    assert actions[0]["payload"]["WeeklySpendLimit"] == 200_000 * M


def test_without_the_addressed_step_the_default_still_clamps():
    # Та же цель без пометки солвера — ±20 % от расхода, как и было.
    actions, _ = diff_budget(
        {"1": _move(960_000, 480_000)},
        {"1": _state(weekly_micros=100_000 * M)},
        {"1": 100_000.0})
    assert actions[0]["payload"]["WeeklySpendLimit"] == 120_000 * M


def test_daily_branch_honours_the_addressed_step():
    # Ручная стратегия: тот же кап, но от дневного расхода.
    actions, _ = diff_budget(
        {"1": _move(960_000, 480_000, write_step=1.0)},
        {"1": _state(daily_micros=14_000 * M)},
        {"1": 100_000.0})
    assert actions[0]["payload"]["DailyBudget"]["Amount"] == 28_571 * M


def test_plan_carries_both_expectations_side_by_side():
    # Два числа, и они не взаимозаменяемы: сырое — мера самой поправки,
    # калиброванное — то, по чему план читается и судится на сжатие.
    calibrated = {"setting_kind": "budget_target",
                  "setting_key": "expected_leads_delta_calibrated",
                  "value": 11.2, "raw_value": 0.5,
                  "support_n": 100, "rel_error": 0.1}
    plan = plan_budget_moves({"1": [_target_row(150_000, 100_000, rel_error=0.05),
                                    _roi_row(1.5, rel_error=0.05),
                                    _expectation_row(22.47), calibrated]})
    assert plan["desired"]["1"]["expected_leads_delta"] == 22.47
    assert plan["desired"]["1"]["expected_leads_delta_calibrated"] == 11.2


def test_journal_keeps_the_raw_expectation_next_to_the_calibrated_one():
    """Сырое ожидание не подменяется поправленным.

    Поправка выведена из сравнения прогноза с исходом. Положи в журнал уже
    поправленное число — и петля начнёт мерить собственную поправку: любое
    смещение сойдётся к единице, ничего не исправив.
    """
    move = _move(720_000, 480_000, leads_delta=6.0)
    move["expected_leads_delta_calibrated"] = 3.0
    actions, _ = diff_budget({"1": move},
                             {"1": _state(weekly_micros=100_000 * M)},
                             {"1": 100_000.0})
    payload = actions[0]["payload"]
    assert payload["expected_leads_delta"] == 6.0
    assert payload["expected_leads_delta_calibrated"] == 3.0
