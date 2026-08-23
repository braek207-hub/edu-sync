# -*- coding: utf-8 -*-
"""Э3.2: единый порог предельной окупаемости и целевые бюджеты кампаний."""

from sync.agent.portfolio import (
    MAX_STEP_DOWN,
    MAX_STEP_UP,
    SWITCH_OFF_ROI_SHARE,
    computed_rows,
    portfolio_targets,
    solve_threshold,
    value_per_lead,
)


def _curve(cost=100000.0, leads=100, beta=0.8, marginal=1250.0,
           direction="spo", rel=0.05):
    return {
        "direction": direction, "cost_28d": cost, "leads_28d": leads,
        "beta": beta, "marginal_cpl": marginal, "marginal_rel_error": rel,
    }


def _ladder(revenue=1000000.0, eff=100, rel=0.1):
    return {"expected_revenue": revenue, "events_by_step": {"eff": eff},
            "rel_error": rel}


# --------------------------------- value_per_lead


def test_value_per_lead_is_revenue_over_eff_events():
    v = value_per_lead(_ladder(revenue=500000.0, eff=50))
    assert v["value"] == 10000.0
    assert v["rel_error"] == 0.1


def test_value_needs_revenue_events_and_error():
    assert value_per_lead({}) is None
    assert value_per_lead(_ladder(revenue=None)) is None
    assert value_per_lead(_ladder(eff=0)) is None
    row = _ladder()
    row["rel_error"] = None
    assert value_per_lead(row) is None


# --------------------------------- solve_threshold


def _solver_row(campaign_id, value, cost=100000.0, beta=0.8, marginal=1250.0):
    return {
        "campaign_id": campaign_id, "direction": "spo", "cost": cost,
        "leads": 100, "beta": beta, "marginal_cpl": marginal,
        "marginal_rel_error": 0.05, "value": value, "value_rel_error": 0.1,
    }


def test_identical_campaigns_keep_their_budgets():
    rows = [_solver_row("1", 5000.0), _solver_row("2", 5000.0)]
    lam, targets = solve_threshold(rows, 200000.0)
    assert abs(targets["1"] - 100000.0) < 100
    assert abs(targets["2"] - 100000.0) < 100
    # Порог сошёлся к текущей предельной окупаемости: value / marginal_cpl.
    assert abs(lam - 4.0) < 0.05


def test_budget_flows_to_higher_value_and_sum_holds():
    rows = [_solver_row("rich", 8000.0), _solver_row("poor", 3000.0)]
    lam, targets = solve_threshold(rows, 200000.0)
    assert targets["rich"] > 100000.0 > targets["poor"]
    assert abs(sum(targets.values()) - 200000.0) < 200


def test_extreme_gap_hits_step_caps():
    rows = [_solver_row("rich", 500000.0), _solver_row("poor", 50.0)]
    _, targets = solve_threshold(rows, 200000.0)
    assert abs(targets["rich"] - 100000.0 * MAX_STEP_UP) < 1
    assert abs(targets["poor"] - 100000.0 * MAX_STEP_DOWN) < 1


def test_unsaturated_campaign_takes_cap_by_current_roi():
    # β ≥ 1: предельная цена с объёмом не растёт — кампания упирается в кап
    # стороны, где её текущая окупаемость относительно порога.
    rows = [_solver_row("flat", 8000.0, beta=1.1), _solver_row("norm", 3000.0)]
    _, targets = solve_threshold(rows, 200000.0)
    assert abs(targets["flat"] - 100000.0 * MAX_STEP_UP) < 1


def test_beta_near_one_does_not_overflow():
    rows = [_solver_row("a", 5100.0, beta=0.999), _solver_row("b", 4900.0)]
    _, targets = solve_threshold(rows, 200000.0)
    assert all(50000.0 <= t <= 150000.0 for t in targets.values())
    assert abs(sum(targets.values()) - 200000.0) < 200


# --------------------------------- portfolio_targets


def _inputs(value_rich=8000.0, value_poor=3000.0):
    saturation = {"1": _curve(), "2": _curve()}
    ladder = {"1": _ladder(revenue=value_rich * 100),
              "2": _ladder(revenue=value_poor * 100)}
    return saturation, ladder


def test_targets_grouped_by_account_with_sum_check():
    saturation = {"1": _curve(), "2": _curve(), "3": _curve(), "4": _curve()}
    ladder = {cid: _ladder() for cid in saturation}
    logins = {"1": "acc-a", "2": "acc-a", "3": "acc-b", "4": "acc-b"}
    section = portfolio_targets(saturation, ladder, logins)
    assert set(section["accounts"]) == {"acc-a", "acc-b"}
    for account in section["accounts"].values():
        assert account["campaigns"] == 2
        assert abs(account["sum_residual"]) < 0.01 * account["budget_28d"]


def test_campaign_without_value_is_fixed_not_silent():
    saturation, ladder = _inputs()
    del ladder["2"]
    section = portfolio_targets(saturation, ladder, {"1": "acc", "2": "acc"})
    account = section["accounts"]["acc"]
    assert section["campaigns_no_value"] == 1
    assert account["fixed"] == {"campaigns": 1, "cost": 100000.0}
    assert list(account["moves"]) == ["1"]
    # Бюджет переносится только между участниками: единственная кампания
    # остаётся при своём.
    assert abs(account["moves"]["1"]["ratio"] - 1.0) < 0.01


def test_moves_carry_direction_and_expected_effect():
    saturation, ladder = _inputs()
    section = portfolio_targets(saturation, ladder, {"1": "acc", "2": "acc"})
    moves = section["accounts"]["acc"]["moves"]
    assert moves["1"]["move"] == "up" and moves["2"]["move"] == "down"
    assert moves["1"]["expected_leads_delta"] > 0 > moves["2"]["expected_leads_delta"]
    # Перенос к дорогому лиду обязан обещать прирост выручки в сумме.
    assert section["accounts"]["acc"]["expected_revenue_delta"] > 0


def test_noisy_estimates_do_not_make_confident_moves():
    saturation, ladder = _inputs()
    for row in ladder.values():
        row["rel_error"] = 2.0
    section = portfolio_targets(saturation, ladder, {"1": "acc", "2": "acc"})
    assert section["accounts"]["acc"]["moves_confident"] == 0


def test_unmapped_campaigns_solve_as_own_group():
    saturation, ladder = _inputs()
    section = portfolio_targets(saturation, ladder, {})
    assert set(section["accounts"]) == {"unmapped"}


def test_computed_rows_carry_both_sides_of_transfer():
    saturation, ladder = _inputs()
    section = portfolio_targets(saturation, ladder, {"1": "acc", "2": "acc"})
    rows = computed_rows(section)
    row = rows["1"][0]
    assert row["setting_kind"] == "budget_target"
    assert row["setting_key"] == "target_28d"
    assert row["value"] == section["accounts"]["acc"]["moves"]["1"]["target_28d"]
    assert row["raw_value"] == 100000.0
    assert row["support_n"] == 100


# --------------------------------- кандидаты на выключение (Э3.4)


def _extreme_inputs():
    # «poor» упирается в пол капа, и даже на полу её предельная окупаемость
    # ничтожна против λ кабинета. «mid» — интерьерная (без капа): она и
    # закрепляет λ; на двух кампаниях, упёршихся в противоположные капы,
    # порог вырождается и сравнивать долю не с чем.
    saturation = {"rich": _curve(), "mid": _curve(), "poor": _curve()}
    ladder = {"rich": _ladder(revenue=500000.0 * 100),
              "mid": _ladder(revenue=5000.0 * 100),
              "poor": _ladder(revenue=50.0 * 100)}
    return saturation, ladder


def test_hopeless_campaign_marked_switch_off():
    saturation, ladder = _extreme_inputs()
    section = portfolio_targets(saturation, ladder,
                                {"rich": "acc", "mid": "acc", "poor": "acc"})
    account = section["accounts"]["acc"]
    poor = account["moves"]["poor"]
    assert abs(poor["ratio"] - MAX_STEP_DOWN) < 0.01
    switch = poor["switch_off"]
    assert switch["roi_share_of_lambda"] < SWITCH_OFF_ROI_SHARE
    assert switch["roi_at_floor"] > 0
    assert account["switch_off_candidates"] == 1
    # Здоровой кампании пометки нет — ключ отсутствует, а не лежит с None.
    assert "switch_off" not in account["moves"]["rich"]


def test_campaign_above_floor_or_share_is_not_candidate():
    # Обе кампании близки по ценности: пола капа никто не достигает.
    saturation, ladder = _inputs()
    section = portfolio_targets(saturation, ladder, {"1": "acc", "2": "acc"})
    account = section["accounts"]["acc"]
    assert account["switch_off_candidates"] == 0
    assert all("switch_off" not in m for m in account["moves"].values())


def test_computed_rows_add_campaign_switch_for_candidate():
    saturation, ladder = _extreme_inputs()
    section = portfolio_targets(saturation, ladder,
                                {"rich": "acc", "mid": "acc", "poor": "acc"})
    rows = computed_rows(section)
    kinds_poor = [r["setting_kind"] for r in rows["poor"]]
    assert kinds_poor == ["budget_target", "campaign_switch"]
    switch_row = rows["poor"][1]
    move = section["accounts"]["acc"]["moves"]["poor"]
    assert switch_row["setting_key"] == "suspend"
    assert switch_row["value"] == move["switch_off"]["roi_share_of_lambda"]
    assert switch_row["raw_value"] == move["switch_off"]["roi_at_floor"]
    assert switch_row["rel_error"] == move["rel_error"]
    assert [r["setting_kind"] for r in rows["rich"]] == ["budget_target"]
