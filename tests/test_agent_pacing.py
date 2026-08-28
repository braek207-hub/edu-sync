# -*- coding: utf-8 -*-
"""Пейсинг месяца: план освоения из потолка, а не из трейлинга-28.

Σ целевых бюджетов была прибита к факту прошедших 28 дней: кабинет всегда
планировал прошлое. Образование сезонно, и это систематическая ошибка — в
подъём агент недоливает, в спад переливает. План месяца задаёт человек
потолком, пейсинг лишь раскладывает остаток по оставшимся дням.
"""

from datetime import date

import pytest

from sync.agent import pacing
from sync.agent.demand import (REGIME_FALL, REGIME_LOW_DATA, REGIME_NORMAL,
                               REGIME_NO_SERIES, REGIME_RISE)
from sync.agent.portfolio import ACCOUNT_GROWTH_STEP, account_budget

SEPT = "2026-09"


def _plan(**over):
    kwargs = {"month": SEPT, "spent_to_date": 0.0, "cap_rub": 3_000_000.0,
              "demand_regime": None, "today": date(2026, 9, 1)}
    kwargs.update(over)
    return pacing.month_plan(**kwargs)


# ------------------------------------------------- план берётся у человека


def test_month_target_comes_from_the_cap_not_from_trailing():
    plan = pacing.month_plan("2026-09", spent_to_date=0.0,
                             cap_rub=20_000_000.0, demand_regime=REGIME_RISE,
                             today=date(2026, 9, 1))

    assert plan["target_rub"] == 20_000_000.0
    assert plan["basis"] == pacing.BASIS_CAP


def test_no_cap_means_no_pacing():
    # Единственный параметр панели, у которого пусто законно: общая сумма
    # кабинета — деньги владельца. Выдуманный план здесь означал бы, что
    # агент назначил себе бюджет сам.
    plan = pacing.month_plan("2026-09", 0.0, None, REGIME_RISE,
                             today=date(2026, 9, 1))

    assert plan["target_rub"] is None
    assert plan["daily_allowance"] is None
    assert plan["basis"] == pacing.BASIS_NO_CAP


def test_the_allowance_spreads_what_is_left_over_the_days_left():
    # 10 сентября истрачен миллион из трёх: остаток 2 000 000 ₽ делится на 21
    # оставшийся день, СЕГОДНЯШНИЙ включительно — день ещё не прожит.
    plan = _plan(spent_to_date=1_000_000.0, today=date(2026, 9, 10))

    assert plan["days_left"] == 21
    assert plan["remaining_rub"] == 2_000_000.0
    assert plan["daily_allowance"] == pytest.approx(2_000_000.0 / 21, abs=0.01)


def test_the_month_is_paced_by_what_is_left_not_by_the_flat_cap():
    # Ровная доля потолка (3 000 000 / 30) не знает, что половина месяца уже
    # прожита впустую: недобор так не догоняется никогда.
    behind = _plan(spent_to_date=500_000.0, today=date(2026, 9, 16))

    assert behind["daily_allowance"] > 3_000_000.0 / 30


def test_spending_ahead_of_plan_never_turns_into_a_negative_allowance():
    # Перерасход — не отрицательные деньги на день, а ноль. Минус здесь уехал
    # бы в потолок окна и прочитался бы как команда резать кабинет.
    plan = _plan(spent_to_date=4_000_000.0, today=date(2026, 9, 20))

    assert plan["remaining_rub"] == 0.0
    assert plan["daily_allowance"] == 0.0


def test_a_closed_month_has_no_days_to_pace():
    plan = _plan(today=date(2026, 10, 1))

    assert plan["days_left"] == 0
    assert plan["daily_allowance"] == 0.0


def test_a_month_not_started_paces_all_of_its_days():
    plan = _plan(today=date(2026, 8, 20))

    assert plan["days_left"] == 30


# ------------------------------------------------- спрос двигает долю дня


def test_demand_regime_shifts_the_daily_share_not_the_cap():
    # Потолок — решение владельца, и рынок его не двигает. Двигается ТЕМП:
    # в подъём деньги нужны сегодня, в спад они дешевле завтра.
    rise = _plan(demand_regime=REGIME_RISE)
    normal = _plan(demand_regime=REGIME_NORMAL)
    fall = _plan(demand_regime=REGIME_FALL)

    assert rise["daily_allowance"] > normal["daily_allowance"] > fall["daily_allowance"]
    assert {p["target_rub"] for p in (rise, normal, fall)} == {3_000_000.0}


def test_the_shifted_share_never_exceeds_what_is_left():
    # Множитель подъёма на последний день месяца обязан упереться в остаток:
    # иначе пейсинг разрешил бы потратить больше потолка.
    plan = _plan(demand_regime=REGIME_RISE, spent_to_date=2_900_000.0,
                 today=date(2026, 9, 30))

    assert plan["daily_allowance"] == 100_000.0


def test_an_unknown_regime_is_paced_evenly():
    # «Мало данных» и «нет ряда» — не подъём и не спад. Считать их спадом
    # значило бы наказывать новое направление за отсутствие истории.
    even = _plan()["daily_allowance"]

    for regime in (REGIME_LOW_DATA, REGIME_NO_SERIES, None, "чепуха"):
        assert _plan(demand_regime=regime)["daily_allowance"] == even, regime


def test_the_dominant_regime_is_the_one_of_the_majority():
    regimes = {"spo": {"regime": REGIME_RISE}, "vpo": {"regime": REGIME_RISE},
               "dist": {"regime": REGIME_FALL},
               "dpo": {"regime": REGIME_NO_SERIES}}

    assert pacing.dominant_regime(regimes) == REGIME_RISE


def test_a_tie_between_rise_and_fall_is_not_a_regime():
    # Половина направлений растёт, половина падает — это не «подъём кабинета»,
    # и темп по такому большинству был бы монеткой.
    regimes = {"spo": {"regime": REGIME_RISE}, "vpo": {"regime": REGIME_FALL}}

    assert pacing.dominant_regime(regimes) == REGIME_NORMAL
    assert pacing.dominant_regime({}) == REGIME_NORMAL


# ------------------------------------------------- потолок окна у солвера


def _pace(daily_allowance):
    return {"target_rub": 3_000_000.0, "daily_allowance": daily_allowance,
            "basis": pacing.BASIS_CAP}


def test_being_behind_the_plan_is_caught_up_within_the_step_cap():
    # Пейсинг разрешает кабинету втрое больше, чем он тратит. Кап шага
    # остаётся в силе: рывок в три раза за такт — это переобучение стратегий
    # кабинета и потеря всей истории, ради которой агент и считает.
    out = account_budget(current_cost=1_000_000.0, lam=3.0, target_romi=2.0,
                         room_rub=500_000.0, monthly_cap=3_000_000.0,
                         pace=_pace(100_000.0))

    assert out["growth_rub"] == round(1_000_000.0 * ACCOUNT_GROWTH_STEP, 2)
    assert out["capped_by"] == "step"


def test_the_pacing_ceiling_replaces_the_flat_monthly_cap():
    # Потолок окна теперь считается из дневной доли пейсинга, а не из
    # месячного потолка ровной долей: кабинет, обогнавший план, роста не
    # получает, даже если формально месячный потолок ещё не выбран.
    out = account_budget(current_cost=1_000_000.0, lam=3.0, target_romi=2.0,
                         room_rub=500_000.0, monthly_cap=3_000_000.0,
                         pace=_pace(37_500.0))

    assert out["budget"] == 1_050_000.0        # 37 500 × 28 дней окна
    assert out["capped_by"] == pacing.CAPPED_BY_PACING


def test_a_pace_below_the_current_spend_never_cuts_the_account():
    # Сокращение общей суммы — решение владельца, а не агента. Упор виден
    # флагом, сумма остаётся.
    out = account_budget(current_cost=1_000_000.0, lam=3.0, target_romi=2.0,
                         room_rub=500_000.0, monthly_cap=3_000_000.0,
                         pace=_pace(1_000.0))

    assert out["budget"] == 1_000_000.0
    assert out["growth_rub"] == 0.0
    assert out["capped_by"] == pacing.CAPPED_BY_PACING


def test_without_a_pace_the_flat_cap_still_rules():
    # Пейсинг считается там, где известен факт месяца; недоступна витрина —
    # поведение прежнее, а не «потолка нет».
    out = account_budget(current_cost=1_000_000.0, lam=3.0, target_romi=2.0,
                         room_rub=500_000.0, monthly_cap=1_141_500.0, pace=None)

    assert out["budget"] == 1_050_000.0
    assert out["capped_by"] == "monthly_cap"


def test_a_pace_without_a_cap_does_not_invent_a_ceiling():
    # Пустой потолок доезжает до солвера планом без цели: прибавка
    # предлагается числом, сумма кабинета не меняется.
    out = account_budget(current_cost=1_000_000.0, lam=3.0, target_romi=2.0,
                         room_rub=500_000.0, monthly_cap=None,
                         pace=pacing.month_plan(SEPT, 0.0, None, REGIME_RISE,
                                                today=date(2026, 9, 10)))

    assert out["budget"] == 1_000_000.0
    assert out["proposed_growth_rub"] == 200_000.0
    assert out["capped_by"] == "step"
