# -*- coding: utf-8 -*-
"""Э3.2: единый порог предельной окупаемости и целевые бюджеты кампаний."""

from sync.agent.portfolio import (
    BETA_SUPERLINEAR_STEP,
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


def test_unsaturated_campaign_steps_gently_not_to_cap():
    # β ≥ 1 — «насыщения не видно» — экстраполяционное утверждение с самым
    # слабым обоснованием из всей кривой (BETA_MAX в saturation его же и
    # зажимает). Прыжок сразу в кап ×1.5 ставил максимальную ставку на
    # слабейшую оценку; шаг умеренный, направление сохраняется, недельный
    # такт волен его повторить.
    rows = [_solver_row("flat", 8000.0, beta=1.1), _solver_row("norm", 3000.0)]
    _, targets = solve_threshold(rows, 200000.0)
    assert abs(targets["flat"] - 100000.0 * BETA_SUPERLINEAR_STEP) < 1
    assert targets["flat"] < 100000.0 * MAX_STEP_UP


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
    assert kinds_poor == ["budget_target", "budget_target", "campaign_switch"]
    switch_row = rows["poor"][2]
    move = section["accounts"]["acc"]["moves"]["poor"]
    assert switch_row["setting_key"] == "suspend"
    assert switch_row["value"] == move["switch_off"]["roi_share_of_lambda"]
    assert switch_row["raw_value"] == move["switch_off"]["roi_at_floor"]
    assert switch_row["rel_error"] == move["rel_error"]
    assert [r["setting_kind"] for r in rows["rich"]] == [
        "budget_target", "budget_target"]


# --------------------------------- находки аудита 2026-08-23


def test_confidence_measures_economic_edge_not_amplified_step():
    # C2: p_sign обязан мерить экономическую гипотезу value против λ·marginal,
    # а не готовый шаг. При β → 1 показатель 1/(1−β) раздувает шаг до капа:
    # ln-разрыв экономики 0.2 на двоих при ошибке ~0.2 стоит z ≈ 0.5, а
    # старый assess(target/cost) видел ln(1.5)/0.2 ≈ 2 и давал «уверенно».
    saturation = {"hot": _curve(beta=0.9, rel=0.14),
                  "cold": _curve(beta=0.9, rel=0.14)}
    ladder = {"hot": _ladder(revenue=1000000.0, rel=0.14),
              "cold": _ladder(revenue=818731.0, rel=0.14)}
    section = portfolio_targets(saturation, ladder,
                                {"hot": "acc", "cold": "acc"})
    acc = section["accounts"]["acc"]
    assert acc["moves_confident"] == 0
    assert acc["moves"]["hot"]["p_sign"] < 0.90
    assert acc["moves"]["hot"]["marginal_roi_vs_lambda"] > 1.0


def test_account_below_marginal_breakeven_is_flagged():
    # C6: λ — окупаемость предельного рубля кабинета. λ < 1 значит предельный
    # рубль возвращает меньше рубля ожидаемой выручки; это состояние обязано
    # стоять в отчёте отдельным флагом, а не прятаться в самом числе λ.
    saturation = {"1": _curve(marginal=1250.0)}
    ladder = {"1": _ladder(revenue=80000.0)}
    section = portfolio_targets(saturation, ladder, {"1": "acc"})
    acc = section["accounts"]["acc"]
    assert acc["lambda"] < 1.0
    assert acc["lambda_breakeven"] is False
    assert section["accounts_below_breakeven"] == ["acc"]

    healthy = portfolio_targets({"1": _curve(marginal=1250.0)},
                                {"1": _ladder(revenue=1000000.0)},
                                {"1": "acc"})
    assert healthy["accounts"]["acc"]["lambda_breakeven"] is True
    assert healthy["accounts_below_breakeven"] == []


def test_computed_rows_carry_roi_vs_lambda():
    # Писатель Э3.3 гейтует уверенность заново из computed-строк — без
    # экономического отношения он мог бы судить только по target/cost, то есть
    # повторял бы раздутый p_sign. Отношение едет отдельной строкой.
    saturation, ladder = _inputs()
    section = portfolio_targets(saturation, ladder, {"1": "acc", "2": "acc"})
    rows = computed_rows(section)["1"]
    roi = next(r for r in rows if r["setting_key"] == "roi_vs_lambda")
    assert roi["setting_kind"] == "budget_target"
    assert roi["value"] == section["accounts"]["acc"]["moves"]["1"]["marginal_roi_vs_lambda"]
    assert roi["raw_value"] == section["accounts"]["acc"]["lambda"]


def test_holdout_campaigns_stay_out_of_the_solver():
    # Заповедник агент не трогает по определению (agent_e1 блокирует действия
    # по его кампаниям). Оставлять их в солвере значит держать порог λ
    # ограничением «сумма целевых = сумме текущих», часть которой никогда не
    # сдвинется: сумма сходится фиктивно, а порог задан неподвижными
    # кампаниями (аудит 2026-08-23).
    saturation, ladder = _inputs()
    free = portfolio_targets(saturation, ladder, {"1": "acc", "2": "acc"})
    guarded = portfolio_targets(saturation, ladder, {"1": "acc", "2": "acc"},
                                holdout_ids={"2"})
    acc = guarded["accounts"]["acc"]
    assert set(acc["moves"]) == {"1"}
    assert acc["holdout"]["campaigns"] == 1
    assert acc["budget_28d"] < free["accounts"]["acc"]["budget_28d"]
    # Бюджет заповедника не участвует и в сумме переноса.
    assert abs(acc["sum_residual"]) < 1.0


def test_holdout_only_account_is_not_reported_as_movable():
    saturation, ladder = _inputs()
    section = portfolio_targets(saturation, ladder, {"1": "acc", "2": "acc"},
                                holdout_ids={"1", "2"})
    assert "acc" not in section["accounts"]


# ------------------- карман исследования


def test_exploration_budget_goes_to_the_least_understood_campaigns():
    # Без разведки кривая насыщения уточняется только там, где история уже
    # что-то говорит, и кампания с неопределённой оценкой остаётся
    # неопределённой навсегда. Часть бюджета распределяется не по λ, а по
    # НЕЗНАНИЮ: чем хуже мы понимаем кривую кампании, тем ценнее узнать.
    from sync.agent.portfolio import exploration_bonus
    campaigns = [
        {"campaign_id": "known", "cost": 100_000.0, "marginal_rel_error": 0.05,
         "value_rel_error": 0.05},
        {"campaign_id": "unknown", "cost": 100_000.0, "marginal_rel_error": 0.40,
         "value_rel_error": 0.40},
    ]
    bonus = exploration_bonus(campaigns, explore_rub=10_000.0)
    assert bonus["unknown"] > bonus["known"]
    assert abs(sum(bonus.values()) - 10_000.0) < 1.0


def test_exploration_budget_is_a_small_share_and_sum_is_kept():
    # Инвариант портфеля не меняется: сумма целевых по-прежнему равна бюджету
    # кабинета — карман берётся ИЗ него, а не сверх него.
    saturation, ladder = _inputs()
    section = portfolio_targets(saturation, ladder, {"1": "acc", "2": "acc"})
    acc = section["accounts"]["acc"]
    assert abs(acc["sum_residual"]) < 1.0
    assert 0 < acc["exploration"]["share"] <= 0.1
    assert acc["exploration"]["rub"] > 0


def test_exploration_skips_campaigns_headed_for_shutdown():
    # Разведка на кампании, которую механизм и так предлагает выключить, —
    # деньги на изучение того, что закрывается.
    from sync.agent.portfolio import exploration_bonus
    campaigns = [
        {"campaign_id": "alive", "cost": 100_000.0, "marginal_rel_error": 0.30,
         "value_rel_error": 0.30},
        {"campaign_id": "dying", "cost": 100_000.0, "marginal_rel_error": 0.90,
         "value_rel_error": 0.90, "switch_off": True},
    ]
    bonus = exploration_bonus(campaigns, explore_rub=10_000.0)
    assert bonus.get("dying", 0.0) == 0.0
    assert abs(bonus["alive"] - 10_000.0) < 1.0
