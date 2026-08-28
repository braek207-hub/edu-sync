# -*- coding: utf-8 -*-
"""
tests/test_agent_writer_expectation.py — ожидание, которое заявляет рычаг
(задача 6 плана беты).

Действие без ожидания нечем ранжировать при отборе и нечем судить в замере:
сторож кладёт рядом с ожиданием факт, и разница этих двух чисел — ЕДИНСТВЕННОЕ,
что делает такт наблюдением, а не движением. До этой задачи ожидание заявляли
два вида действий из девяти — budget.set и budget.set_daily.
"""

import pytest

from sync.agent.writer import expectation, exposure, guardrails, lanes
from sync.agent.writer.budget import diff_budget
from sync.agent.writer.diff import diff_modifiers, diff_schedule
from sync.agent.writer.goal import diff_goal
from sync.agent.writer.strategy import diff_strategy
from sync.agent.writer.negatives import (diff_negatives, plan_negatives,
                                         remove_added_action)
from sync.agent.writer.placements import diff_placements, plan_placements
from sync.agent.writer.schedule import schedule_items
from sync.agent.writer.switch import diff_switch, plan_switch_offs
from sync.agent.writer.tcpa import diff_tcpa, plan_tcpa_moves

M = 1_000_000

# Экономика объекта, которой в самом действии нет: расход в день и цена лида.
# Числа порядка боевого кабинета EDU (12 000 ₽/дн при CPL 2 400 ₽).
CTX = {"daily_cost_rub": 12_000.0, "cpa_rub": 2_400.0}


# ------------------------------------------------------------ образцы действий


def _bidmodifier(kind="bidmodifier.add", percent=-30, share=0.25):
    desired = [{"kind": "bid_modifier:device", "direct_type": "MOBILE_ADJUSTMENT",
                "key": "MOBILE", "percent": percent, "share": share}]
    actual = ([] if kind == "bidmodifier.add"
              else [{"Id": 7, "Type": "MOBILE_ADJUSTMENT", "key": "MOBILE",
                     "percent": 0}])
    return diff_modifiers(desired, actual, "111", context=CTX)[0]


def _schedule():
    items = schedule_items([{"setting_key": "3", "value": -40},
                            {"setting_key": "4", "value": -40}])
    return diff_schedule(items, {}, "111", context=CTX)[0]


def _negative(cut_cost=9_000.0, conversions=0):
    plan = plan_negatives([{"query": "мгсу", "cost": cut_cost, "clicks": 300,
                            "conversions": conversions, "reason": "zero_conversions",
                            "campaigns": ["1"],
                            "cost_by_campaign": {"1": cut_cost}}])
    actions, _ = diff_negatives(
        plan["desired"], {"1": {"negative_keywords": [],
                                "campaign_type": "TEXT_CAMPAIGN"}},
        cut_cost=plan["cut_cost"], cut_conversions=plan["cut_conversions"])
    return actions[0]


def _placement(cut_cost=9_000.0, conversions=0):
    plan = plan_placements([{"placement": "games.example.com", "cost": cut_cost,
                             "clicks": 300, "conversions": conversions,
                             "reason": "zero_conversions", "campaigns": ["1"],
                             "cost_by_campaign": {"1": cut_cost}}])
    actions, _ = diff_placements(
        plan["desired"], {"1": {"excluded_sites": [],
                                "campaign_type": "TEXT_CAMPAIGN"}},
        cut_cost=plan["cut_cost"], cut_conversions=plan["cut_conversions"])
    return actions[0]


def _strategy(weekly_micros=None, target_micros=None):
    holder = {"GoalId": 42}
    if weekly_micros is not None:
        holder["WeeklySpendLimit"] = weekly_micros
    if target_micros is not None:
        holder["AverageCpa"] = target_micros
    return {"Search": {"BiddingStrategyType": "AVERAGE_CPA", "AverageCpa": holder},
            "Network": {"BiddingStrategyType": "SERVING_OFF"}}


def _budget(daily=False, leads_delta=22.47):
    move = {"target_28d": 480_000.0, "cost_28d": 400_000.0, "ratio": 1.2,
            "p_sign": 0.99, "expected_leads_delta": leads_delta}
    state = {"campaign_type": "TEXT_CAMPAIGN",
             "strategy": (_strategy() if daily else _strategy(weekly_micros=80_000 * M)),
             "daily_budget": {"Amount": 12_000 * M, "Mode": "STANDARD"} if daily else None,
             "package_id": None}
    actions, _ = diff_budget({"1": move}, {"1": state},
                             weekly_spend_no_vat={"1": 80_000.0})
    return actions[0]


def _tcpa_rows(target=1_300.0, current=1_000.0):
    return [
        {"setting_kind": "tcpa_target", "setting_key": "target",
         "value": target, "raw_value": current, "support_n": 100, "rel_error": 0.05},
        {"setting_kind": "tcpa_target", "setting_key": "roi_vs_target",
         "value": 1.4, "raw_value": 1_500.0, "support_n": 100, "rel_error": 0.05},
        {"setting_kind": "budget_target", "setting_key": "target_28d",
         "value": 360_000.0, "raw_value": 336_000.0, "support_n": 140,
         "rel_error": 0.05},
    ]


def _tcpa():
    plan = plan_tcpa_moves({"1": _tcpa_rows()})
    state = {"1": {"campaign_type": "TEXT_CAMPAIGN", "package_id": None,
                   "strategy": _strategy(target_micros=1_000 * M)}}
    actions, _ = diff_tcpa(plan["desired"], state)
    return actions[0]


def _goal():
    """Смена цели: конверсия новой цели снята с другого объекта — это ставка."""
    strategy = {"Search": {"BiddingStrategyType": "AVERAGE_CPA",
                           "AverageCpa": {"PriorityGoals": [{"GoalId": 42}],
                                          "AverageCpa": 2_400 * M}},
                "Network": {"BiddingStrategyType": "SERVING_OFF"}}
    actions, _ = diff_goal(
        {"1": {"goal_ids": [541_664_134], "reaches": {541_664_134: 400.0},
               "window_days": 28, "clicks_per_day": 120.0,
               "cr_current": 0.020, "cr_new": 0.026}},
        {"1": {"campaign_type": "TEXT_CAMPAIGN", "package_id": None,
               "strategy": strategy}})
    return actions[0]


def _strategy_switch():
    """Смена стратегии: конверсия под новой стратегией перенесена с соседа."""
    actions, _ = diff_strategy(
        {"1": {"strategy_type": "AVERAGE_CPA", "goal_ids": [541_664_134],
               "reaches": {541_664_134: 400.0}, "window_days": 28,
               "target_cpa": 2_400.0, "weekly_limit": 80_000.0,
               "clicks_per_day": 120.0, "cr_current": 0.020, "cr_new": 0.026}},
        {"1": {"campaign_type": "TEXT_CAMPAIGN", "package_id": None,
               "daily_budget": {"Amount": 12_000 * M},
               "strategy": {"Search": {"BiddingStrategyType": "HIGHEST_POSITION"},
                            "Network": {"BiddingStrategyType": "SERVING_OFF"}}}})
    return actions[0]


def _switch_rows(roi_share=0.3):
    return [
        {"setting_kind": "campaign_switch", "setting_key": "suspend",
         "value": roi_share, "raw_value": roi_share * 4.0, "support_n": 100,
         "rel_error": 0.05, "calc_date": "2026-08-23"},
        {"setting_kind": "budget_target", "setting_key": "target_28d",
         "value": 300_000.0, "raw_value": 336_000.0, "support_n": 140,
         "rel_error": 0.05},
    ]


def _suspend():
    plan = plan_switch_offs({"1": _switch_rows()})
    actions, _ = diff_switch(plan["desired"], {"1": "ON"})
    return actions[0]


def _remove_added():
    """Снятие своей минус-фразы: числа — зеркало отменяемого отсечения."""
    return remove_added_action(
        "111", current=["бесплатно", "колледж заочно москва"],
        added=["колледж заочно москва"],
        restores={"restored_daily_rub": 300.0,
                  "restored_conversions_per_day": 0.4})


_SAMPLES = {
    "bidmodifier.add": lambda: _bidmodifier("bidmodifier.add"),
    "bidmodifier.set": lambda: _bidmodifier("bidmodifier.set"),
    "schedule.set": _schedule,
    "budget.set": lambda: _budget(daily=False),
    "budget.set_daily": lambda: _budget(daily=True),
    "campaign.suspend": _suspend,
    "tcpa.set": _tcpa,
    "goal.set": _goal,
    "strategy.set": _strategy_switch,
    "negative.add": _negative,
    "negative.remove_added": _remove_added,
    "placement.exclude": _placement,
}


def _sample_action(kind):
    action = _SAMPLES[kind]()
    assert action["action_kind"] == kind, action["action_kind"]
    return action


# ------------------------------------------------- ни одно действие без ожидания


@pytest.mark.parametrize("kind", sorted(guardrails.ALLOWED_ACTION_KINDS))
def test_every_applied_kind_declares_an_expectation(kind):
    action = _sample_action(kind)
    assert (action.get("payload") or {}).get("expected_leads_delta") is not None, kind


@pytest.mark.parametrize("kind", sorted(guardrails.ALLOWED_ACTION_KINDS))
def test_every_applied_kind_states_its_horizon(kind):
    """Ожидание без срока непроверяемо: неизвестно, когда класть рядом факт.

    Срок — из полос (lanes.MEASURE_DAYS), а не своя константа рычага: замер
    такта и лимит полосы обязаны мерить одно и то же окно.
    """
    action = _sample_action(kind)
    exp = expectation.of(action, CTX)
    assert exp["measure_days"] == lanes.MEASURE_DAYS[lanes.lane_of(action)]


# ------------------------------------------------------- вырезающее действие


def test_cutting_action_expects_less_spend_and_no_fewer_leads():
    exp = expectation.of(_negative(cut_cost=9_000.0, conversions=0), CTX)
    assert exp["rub_delta"] < 0
    assert exp["leads_delta"] >= 0


def test_cut_of_converting_traffic_admits_the_loss():
    """Кандидат с конверсиями режется по ЦЕНЕ конверсии, а не по их отсутствию.

    Такое отсечение лиды теряет, и заявить «лидов не потеряем» здесь значило
    бы сделать наблюдение заведомо провальным: факт разошёлся бы с ожиданием
    на ровном месте.
    """
    exp = expectation.of(_negative(cut_cost=30_000.0, conversions=6), CTX)
    assert exp["leads_delta"] < 0


def test_excluded_placement_expects_less_spend():
    exp = expectation.of(_placement(), CTX)
    assert exp["rub_delta"] < 0


# ------------------------------------------------------------------ основание


def test_expectation_states_its_basis():
    assert expectation.of(_sample_action("bidmodifier.set"), CTX)["basis"]


@pytest.mark.parametrize("kind", sorted(guardrails.ALLOWED_ACTION_KINDS))
def test_basis_is_never_empty(kind):
    # Без основания число непроверяемо: неизвестно, посчитана эластичность
    # сегмента или взят дефолт.
    assert expectation.of(_sample_action(kind), CTX)["basis"].strip()


# --------------------------------------------------------- модель по рычагам


def test_bid_modifier_expects_more_leads_for_the_same_money():
    """Корректировка не двигает лимит: деньги переносятся ВНУТРИ объекта.

    Обещание сегментной правки — не «дешевле» и не «больше расход», а «те же
    деньги, больше лидов»: доля сегмента уходит из конверсии хуже базовой в
    базовую.
    """
    exp = expectation.of(_bidmodifier(percent=-30, share=0.25), CTX)
    # 12 000 ₽/дн × 0.25 доли × 0.30 сдвига = 900 ₽/дн переносится;
    # 900 × 0.30 / 2 400 ₽ = 0.1125 лида в день × 7 дней замера.
    assert exp["leads_delta"] == pytest.approx(0.79, abs=0.01)
    assert exp["rub_delta"] == 0.0


def test_bid_modifier_up_and_down_promise_the_same_gain():
    # Знак корректировки — это направление переноса, а не знак обещания:
    # и доливка в сильный сегмент, и урезание слабого дают ПРИРОСТ лидов.
    up = expectation.of(_bidmodifier(percent=30, share=0.25), CTX)
    down = expectation.of(_bidmodifier(percent=-30, share=0.25), CTX)
    assert up["leads_delta"] == down["leads_delta"] > 0


def test_suspend_expects_fewer_leads_and_less_spend():
    # Выключение — единственный рычаг, который лиды теряет по построению:
    # его оправдание в том, что деньги работают лучше в соседней кампании,
    # а не в том, что кампания что-то принесёт.
    exp = expectation.of(_suspend(), CTX)
    assert exp["leads_delta"] < 0
    assert exp["rub_delta"] < 0


def test_tcpa_up_expects_more_spend_and_more_leads():
    exp = expectation.of(_tcpa(), CTX)
    assert exp["rub_delta"] > 0
    assert exp["leads_delta"] > 0


def test_schedule_expects_gain_without_moving_the_limit():
    exp = expectation.of(_schedule(), CTX)
    assert exp["leads_delta"] > 0
    assert exp["rub_delta"] == 0.0


# ------------------------------------------------- обратная совместимость


def test_budget_expectation_unchanged():
    """Бюджетный рычаг отдаёт РОВНО число солвера, а не пересчитанное здесь.

    По нему считается поправка прогноза (expected_leads_delta_calibrated) и
    судится каждый закрытый такт: пересчитай его вторая модель — калибровка
    мерила бы разницу двух моделей, а не промах одной.
    """
    for daily in (False, True):
        action = _budget(daily=daily, leads_delta=22.47)
        assert action["payload"]["expected_leads_delta"] == 22.47
        assert expectation.of(action, CTX)["leads_delta"] == 22.47


def test_budget_without_solver_row_has_no_expectation():
    # Ноль означал бы прогноз «эффекта не будет», и петля обучения зачла бы
    # его как сбывшийся.
    action = _budget(leads_delta=None)
    assert expectation.of(action, CTX) is None


# --------------------------------------------- молчание вместо выдуманного нуля


def test_no_expectation_when_object_economics_unknown():
    """Без цены лида корректировка ожидания не заявляет.

    Ноль здесь был бы не «осторожной оценкой», а прогнозом «эффекта не
    будет» — и петля обучения зачла бы его сбывшимся.
    """
    action = diff_modifiers(
        [{"kind": "bid_modifier:device", "direct_type": "MOBILE_ADJUSTMENT",
          "key": "MOBILE", "percent": -30, "share": 0.25}],
        [], "111")[0]
    assert "expected_leads_delta" not in (action.get("payload") or {})
    assert expectation.of(action, None) is None


def test_no_expectation_when_segment_share_unknown():
    # Доля сегмента не установлена: цена риска в таком случае берёт весь
    # объект (консервативно), а обещание — наоборот, не выдаётся вовсе.
    action = diff_modifiers(
        [{"kind": "bid_modifier:device", "direct_type": "MOBILE_ADJUSTMENT",
          "key": "MOBILE", "percent": -30}],
        [], "111", context=CTX)[0]
    assert expectation.of(action, CTX) is None


def test_unknown_kind_gets_no_expectation_instead_of_a_crash():
    # Вид без модели ожидания — молчание с None. Падение здесь уронило бы
    # прогон целиком из-за одного незнакомого вида.
    assert expectation.of({"action_kind": "proposal.campaign", "payload": {}},
                          CTX) is None


# --- обещание отсечения читает сырой расход, а не уценённый ----------------


def _raw_cut_action(exposure_block):
    """Отсечение ДО attach: заявленного ожидания ещё нет, считается модель.

    Именно в этот момент — между построением экспозиции и attach в
    writer/negatives — и решается, из какого числа выйдет обещание.
    """
    return {"action_kind": "negative.add", "object_level": "campaign",
            "object_id": "1", "exposure": exposure_block,
            "payload": {"CampaignId": 1, "AddedPhrases": ["мгсу"]}}


def test_cut_promise_is_measured_by_removed_money_not_by_risked_money():
    """Дефект на стыке задач 8 и 8а: обещание занижалось ровно на скидку.

    exposure.cut_exposure кладёт в daily_rub долю правила трёх — деньги под
    ударом, — а снимается с кабинета весь поток. Читай обещание из daily_rub,
    и рычаг заявил бы «сниму 71 ₽/дн» там, где снимет 1071, а замер такта
    записал бы ему промах в пятнадцать раз за собственную же скидку движка.
    """
    priced = exposure.cut_exposure(cut_cost_rub=30_000.0, conversions=0,
                                   window_days=28)
    assert priced["daily_rub"] < priced["cut_daily_rub"]  # скидка есть

    exp = expectation.of(_raw_cut_action(priced), CTX)

    assert exp["rub_delta"] == -round(priced["cut_daily_rub"] * exp["measure_days"], 2)


def test_cut_promise_falls_back_to_old_exposure_shape():
    # Экспозиция без cut_daily_rub (traffic_cut_exposure) — там скидки нет,
    # и daily_rub означает то же самое. Запасной путь обязан работать: иначе
    # рычаги, не переведённые на новую цену, разом остались бы без обещания.
    old_shape = {"daily_rub": 321.43, "basis": ""}

    exp = expectation.of(_raw_cut_action(old_shape), CTX)

    assert exp["rub_delta"] == -round(321.43 * exp["measure_days"], 2)
