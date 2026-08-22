# -*- coding: utf-8 -*-
from math import isclose

from sync.agent.ladder import (
    NO_STEP_REASON,
    choose_step,
    ladder,
    ladder_report,
    transition_rate,
)

# Пул уровня направления: воронка с типовыми переходами
# 10000 кликов → 1000 лидов → 900 эфф → 400 соединений → 200 сделок → 30 оплат.
DIRECTION_POOL = {"clicks": 10000, "leads": 1000, "eff": 900,
                  "connected": 400, "deals": 200, "paid": 30}
ACCOUNT_POOL = {"clicks": 100000, "leads": 10000, "eff": 9000,
                "connected": 4000, "deals": 2000, "paid": 300}
POOLS = (("direction:spo", DIRECTION_POOL), ("account", ACCOUNT_POOL))


# ---------------------------------------------------------------- выбор ступени

def test_chooses_deepest_step_with_enough_events():
    assert choose_step({"paid": 3, "deals": 10, "connected": 30, "eff": 200}) == "connected"


def test_paid_step_wins_when_it_has_volume():
    assert choose_step({"paid": 25, "deals": 100}) == "paid"


def test_no_step_when_everything_thin():
    assert choose_step({"paid": 1, "deals": 4, "connected": 9,
                        "eff": 20, "leads": 24, "clicks": 24.9}) is None


def test_missing_keys_read_as_zero():
    assert choose_step({"clicks": 500}) == "clicks"


# --------------------------------------------------------- коэффициенты пулов

def test_rate_from_first_reliable_pool():
    rate = transition_rate("paid", "deals", POOLS)
    assert rate["source"] == "direction:spo"
    assert isclose(rate["rate"], 30 / 200)
    assert not rate["weak"]


def test_rate_falls_back_to_general_pool_when_numerator_thin():
    thin_direction = dict(DIRECTION_POOL, paid=5)
    rate = transition_rate("paid", "deals", (("direction:new", thin_direction),
                                             ("account", ACCOUNT_POOL)))
    assert rate["source"] == "account"
    assert not rate["weak"]


def test_rate_weak_when_no_pool_is_reliable():
    thin = {"paid": 5, "deals": 100}
    rate = transition_rate("paid", "deals", (("direction:new", thin),))
    assert rate["weak"] is True
    assert isclose(rate["rate"], 0.05)


def test_rate_none_when_denominator_empty_everywhere():
    assert transition_rate("paid", "deals", (("direction:x", {"paid": 0, "deals": 0}),)) is None


# ----------------------------------------------------------------- пересчёт

def test_ladder_translates_step_to_expected_payments():
    # Ступень eff: коэффициент = (30/200)·(200/400)·(400/900) = 1/30.
    result = ladder({"eff": 300, "connected": 20, "deals": 5, "paid": 1}, POOLS)
    assert result["step"] == "eff"
    assert isclose(result["expected_payments"], 300 / 30, rel_tol=1e-6)
    assert result["weak_rates"] == 0


def test_ladder_on_paid_step_needs_no_rates():
    result = ladder({"paid": 30}, POOLS)
    assert result["step"] == "paid"
    assert result["to_payments_coeff"] == 1.0
    assert result["expected_payments"] == 30
    assert result["rates"] == []


def test_ladder_reports_error_of_step_and_rates():
    # Оплата напрямую: ошибка только ступени, 1/√25 = 0.2.
    direct = ladder({"paid": 25}, POOLS)
    assert isclose(direct["rel_error"], 0.2)
    # Через пересчёт ошибка строго больше: добавляются ошибки коэффициентов.
    translated = ladder({"eff": 25}, POOLS)
    assert translated["rel_error"] > direct["rel_error"]


def test_ladder_revenue_via_avg_check():
    result = ladder({"paid": 30}, POOLS, avg_check=100000)
    assert result["expected_revenue"] == 3000000


def test_ladder_without_any_step_gives_reason_not_zero():
    result = ladder({"clicks": 10}, POOLS)
    assert result["step"] is None
    assert result["reason"] == NO_STEP_REASON
    assert "expected_payments" not in result


def test_ladder_honest_refusal_when_transition_unmeasurable():
    # Пул без сделок вовсе: переход deals→paid не оценить.
    empty_pool = (("direction:x", {"deals": 0, "paid": 0, "connected": 500, "eff": 900}),)
    result = ladder({"connected": 100}, empty_pool)
    assert result["expected_payments"] is None
    assert "deals" in result["reason"]


# ------------------------------------------------------------------- сводка

def test_report_distribution_counts_steps():
    objects = {
        "a": {"paid": 30},
        "b": {"eff": 300},
        "c": {"clicks": 3},
    }
    pools = {obj: POOLS for obj in objects}
    report = ladder_report(objects, pools)
    assert report["distribution"] == {"paid": 1, "eff": 1, "нет_ступени": 1}
    assert report["without_step"] == ["c"]
    assert report["by_object"]["b"]["step"] == "eff"


def test_report_passes_avg_check_per_object():
    report = ladder_report({"a": {"paid": 30}}, {"a": POOLS},
                           avg_check_by_object={"a": 50000})
    assert report["by_object"]["a"]["expected_revenue"] == 1500000
