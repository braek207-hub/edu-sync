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
    # По ключу, а не по индексу: строки budget_target прибавляются по мере
    # того, как писателю нужны новые числа солвера, и тест, привязанный к
    # позиции, краснел бы на каждой такой прибавке, ничего не проверяя.
    switch_rows = [r for r in rows["poor"] if r["setting_kind"] == "campaign_switch"]
    assert len(switch_rows) == 1
    switch_row = switch_rows[0]
    move = section["accounts"]["acc"]["moves"]["poor"]
    assert switch_row["setting_key"] == "suspend"
    assert switch_row["value"] == move["switch_off"]["roi_share_of_lambda"]
    assert switch_row["raw_value"] == move["switch_off"]["roi_at_floor"]
    assert switch_row["rel_error"] == move["rel_error"]
    assert not [r for r in rows["rich"] if r["setting_kind"] == "campaign_switch"]


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


# --------------------------------- недобор трафика в кармане разведки


def _settings(weekly=None, channel="search", daily=None,
              campaign_type="TEXT_CAMPAIGN", package=None):
    """Строка edu_campaign_settings в форме sync/edu_direct_settings.py:632."""
    strategy = {"search": None, "network": None,
                "dailyBudget": daily, "package": package}
    if weekly is not None:
        strategy[channel] = {"biddingStrategyType": "AVERAGE_CPA",
                             "weeklyBudget": weekly}
    return {"meta": {"campaignType": campaign_type}, "strategy": strategy}


# Расход 100 000 ₽ за 28 дней с НДС — это 20 833 ₽ в неделю без НДС.
BINDING_WEEKLY = 20_000.0      # расход добирает до лимита → лимит связывает
LOOSE_WEEKLY = 200_000.0       # лимит висит в разы выше расхода → декорация


def test_binding_limit_is_spend_against_the_weekly_limit():
    from sync.agent.portfolio import binding_limit

    assert binding_limit(_settings(weekly=BINDING_WEEKLY), 100_000.0) is True
    assert binding_limit(_settings(weekly=LOOSE_WEEKLY), 100_000.0) is False
    # Тот же лимит в сетевом канале читается так же: канал ровно один.
    assert binding_limit(_settings(weekly=BINDING_WEEKLY, channel="network"),
                         100_000.0) is True


def test_binding_limit_falls_back_to_the_daily_budget():
    # Ручная стратегия: недельного лимита нет, регулятор — DailyBudget
    # (та же ветка, что в writer/budget.py::diff_budget).
    from sync.agent.portfolio import binding_limit

    assert binding_limit(_settings(daily=3_000.0), 100_000.0) is True
    assert binding_limit(_settings(daily=30_000.0), 100_000.0) is False


def test_limit_is_not_binding_where_the_lever_refuses():
    # Ровно те отказы, которые писатель Э3.3 выдаёт на повышение: пакетная
    # стратегия, не TEXT_CAMPAIGN, лимит в обоих каналах, лимита нет вовсе.
    from sync.agent.portfolio import binding_limit

    package = _settings(weekly=BINDING_WEEKLY)
    package["strategy"]["package"] = {"id": 777}
    assert binding_limit(package, 100_000.0) is False

    assert binding_limit(_settings(weekly=BINDING_WEEKLY,
                                   campaign_type="UNIFIED_CAMPAIGN"),
                         100_000.0) is False

    both = _settings(weekly=BINDING_WEEKLY)
    both["strategy"]["network"] = {"biddingStrategyType": "NETWORK_DEFAULT",
                                   "weeklyBudget": BINDING_WEEKLY}
    assert binding_limit(both, 100_000.0) is False

    assert binding_limit(_settings(), 100_000.0) is False
    assert binding_limit(None, 100_000.0) is False


def test_exploration_prefers_campaigns_with_traffic_headroom():
    from sync.agent.portfolio import exploration_bonus

    base = {"value_rel_error": 0.2, "marginal_rel_error": 0.2, "cost": 100_000.0,
            "limit_binding": True}
    campaigns = [
        {"campaign_id": "111", **base, "headroom_share": 0.6},
        {"campaign_id": "222", **base, "headroom_share": 0.0},
    ]
    bonus = exploration_bonus(campaigns, explore_rub=16_000.0)
    # Незнание и расход равны, отличается только недобор: 1.6 против 1.0.
    assert round(bonus["111"] / bonus["222"], 2) == 1.6
    assert round(bonus["111"] + bonus["222"], 2) == 16_000.0


def test_exploration_without_headroom_is_unchanged():
    from sync.agent.portfolio import exploration_bonus

    campaigns = [
        {"campaign_id": "111", "value_rel_error": 0.2, "marginal_rel_error": 0.0,
         "cost": 100_000.0},
        {"campaign_id": "222", "value_rel_error": 0.1, "marginal_rel_error": 0.0,
         "cost": 100_000.0},
    ]
    bonus = exploration_bonus(campaigns, explore_rub=3_000.0)
    assert round(bonus["111"] / bonus["222"], 2) == 2.0


def test_exploration_ignores_headroom_where_the_limit_does_not_bind():
    # Надбавка — это ДЕНЬГИ, а деньги доезжают только туда, где лимит
    # связывает расход (9 кампаний из 62). Кампания с недобором и висящим
    # лимитом прибавку просто не выберет: вес поднимать не за что.
    from sync.agent.portfolio import exploration_bonus

    base = {"value_rel_error": 0.2, "marginal_rel_error": 0.2, "cost": 100_000.0,
            "headroom_share": 0.6}
    campaigns = [
        {"campaign_id": "111", **base, "limit_binding": True},
        {"campaign_id": "222", **base, "limit_binding": False},
    ]
    bonus = exploration_bonus(campaigns, explore_rub=16_000.0)
    assert round(bonus["111"] / bonus["222"], 2) == 1.6


def test_portfolio_carries_headroom_and_binding_from_the_settings_vitrine():
    saturation = {"1": _curve(), "2": _curve()}
    for row in saturation.values():
        row["headroom_share"] = 0.6
    ladder = {cid: _ladder() for cid in saturation}
    section = portfolio_targets(
        saturation, ladder, {"1": "acc", "2": "acc"},
        settings_by_campaign={"1": _settings(weekly=BINDING_WEEKLY),
                              "2": _settings(weekly=LOOSE_WEEKLY)})
    account = section["accounts"]["acc"]
    # Недобор есть у обеих, разведочную надбавку он поднимает у одной.
    assert account["exploration"]["headroom_boosted"] == 1


def test_portfolio_without_settings_boosts_nobody():
    # Настроек нет — «связывает ли лимит» неизвестно, и множитель недобора
    # не применяется: надбавка по незнанию, но без выдуманного признака.
    saturation = {"1": _curve(), "2": _curve()}
    for row in saturation.values():
        row["headroom_share"] = 0.6
    ladder = {cid: _ladder() for cid in saturation}
    section = portfolio_targets(saturation, ladder, {"1": "acc", "2": "acc"})
    assert section["accounts"]["acc"]["exploration"]["headroom_boosted"] == 0


# --------------------------------- адресный шаг ×2 (кап шага вверх)


def _big_step_campaign(**over):
    """Кампания, у которой сошлись все четыре условия расширенного капа."""
    base = {"growth_room": True, "beta": 0.6, "marginal_roi_vs_lambda": 1.6,
            "limit_binding": True}
    base.update(over)
    return base


def test_step_cap_is_1_5_by_default():
    from sync.agent.portfolio import step_cap_up

    assert step_cap_up(_big_step_campaign(growth_room=False,
                                          marginal_roi_vs_lambda=3.0)) == 1.5


def test_step_cap_is_2_when_headroom_and_economics_agree():
    from sync.agent.portfolio import step_cap_up

    assert step_cap_up(_big_step_campaign()) == 2.0


def test_step_cap_stays_1_5_without_headroom_proof():
    # Экономика хорошая, но объём брать негде: ×2 просто поднимет цену клика.
    # None — «не мерили», и это не то же самое, что измеренный ноль.
    from sync.agent.portfolio import step_cap_up

    assert step_cap_up(_big_step_campaign(growth_room=None,
                                          marginal_roi_vs_lambda=3.0)) == 1.5


def test_step_cap_stays_1_5_when_curve_is_superlinear():
    from sync.agent.portfolio import step_cap_up

    assert step_cap_up(_big_step_campaign(beta=1.2,
                                          marginal_roi_vs_lambda=3.0)) == 1.5


def test_step_cap_stays_1_5_on_thin_margin():
    from sync.agent.portfolio import step_cap_up

    assert step_cap_up(_big_step_campaign(marginal_roi_vs_lambda=1.1)) == 1.5


def test_step_cap_stays_1_5_where_the_limit_does_not_bind():
    # Замер рычага: «вверх» лимитом применимо к 9 кампаниям из 62. У
    # остальных поднятый потолок не купит ни одного показа — писатель
    # откажет, а солвер уже посчитал бы эти деньги распределёнными.
    from sync.agent.portfolio import step_cap_up

    assert step_cap_up(_big_step_campaign(limit_binding=False,
                                          marginal_roi_vs_lambda=3.0)) == 1.5


def _twin(campaign_id, **over):
    row = _solver_row(campaign_id, 5000.0)
    row.update(over)
    return row


def test_solver_grants_double_step_only_to_the_addressed_campaign():
    # Экономика у обеих одна и та же — отличается только доказанный недобор.
    rows = [_twin("addressed", growth_room=True, limit_binding=True),
            _twin("plain")]
    _, targets = solve_threshold(rows, 350_000.0)
    assert abs(targets["addressed"] - 100_000.0 * 2.0) < 1
    assert abs(targets["plain"] - 100_000.0 * MAX_STEP_UP) < 1


def _addressed_inputs():
    # «anchor» несёт бюджет кабинета и держит λ; «rich» упирается в кап
    # вверх, и только у неё сошлись условия расширенного шага.
    rich = _curve()
    rich["headroom_share"] = 0.6
    rich["growth_room"] = True
    saturation = {"rich": rich, "anchor": _curve(cost=1_000_000.0, leads=1000)}
    ladder = {"rich": _ladder(revenue=50_000.0 * 100),
              "anchor": _ladder(revenue=5_000.0 * 1000, eff=1000)}
    return saturation, ladder


def test_move_carries_write_step_for_the_addressed_campaign():
    saturation, ladder = _addressed_inputs()
    section = portfolio_targets(
        saturation, ladder, {"rich": "acc", "anchor": "acc"},
        settings_by_campaign={"rich": _settings(weekly=BINDING_WEEKLY)})
    moves = section["accounts"]["acc"]["moves"]
    # Кап записи для этой кампании — ±100 % от расхода, а не общие ±20 %.
    assert moves["rich"]["write_step"] == 1.0
    assert "write_step" not in moves["anchor"]


def test_exploration_ceiling_follows_the_addressed_cap_not_the_common_one():
    # Карман разведки изымает долю у всех, в том числе у кампании с правом
    # на ×2, и обратно возвращает только её долю кармана — до ровных ×2 она
    # не дотягивает, и это правильно: 7 % бюджета кабинета уходят на разведку.
    # Проверяется другое: потолок РАЗДАЧИ адресный. Будь он общим ×1.5, у неё
    # room выходил бы нулём (цель уже выше потолка), деньги возвращались бы
    # источникам, и расширенный шаг съезжал бы под общий кап — молча, потому
    # что сумма кабинета при этом сходится и ни один инвариант не краснеет.
    saturation, ladder = _addressed_inputs()
    section = portfolio_targets(
        saturation, ladder, {"rich": "acc", "anchor": "acc"},
        settings_by_campaign={"rich": _settings(weekly=BINDING_WEEKLY)})
    account = section["accounts"]["acc"]
    assert account["exploration"]["rub"] > 0
    assert account["moves"]["rich"]["ratio"] > MAX_STEP_UP
    assert abs(account["sum_residual"]) < 0.01 * account["budget_28d"]


def test_computed_rows_carry_the_addressed_write_step():
    saturation, ladder = _addressed_inputs()
    section = portfolio_targets(
        saturation, ladder, {"rich": "acc", "anchor": "acc"},
        settings_by_campaign={"rich": _settings(weekly=BINDING_WEEKLY)})
    rows = computed_rows(section)
    step_rows = [r for r in rows["rich"] if r["setting_key"] == "write_step"]
    assert len(step_rows) == 1
    assert step_rows[0]["setting_kind"] == "budget_target"
    assert step_rows[0]["value"] == 1.0
    assert not [r for r in rows["anchor"] if r["setting_key"] == "write_step"]


def test_move_names_the_lever_and_the_upper_cap_it_reached():
    """Строка сдвига обязана нести повод роста, а не только его величину.

    Список усиления (agent/growth.py) собирается из этих двух полей: без
    limit_binding он предлагал бы долить денег 53 кампаниям из 62, где лимит
    расход не связывает и прибавка не превратится ни в один показ, а без
    step_capped молчал бы о кампаниях, которым солвер хотел дать больше, чем
    разрешено за такт.
    """
    saturation, ladder = _addressed_inputs()
    section = portfolio_targets(
        saturation, ladder, {"rich": "acc", "anchor": "acc"},
        settings_by_campaign={"rich": _settings(weekly=BINDING_WEEKLY)})
    moves = section["accounts"]["acc"]["moves"]

    assert moves["rich"]["limit_binding"] is True
    assert moves["rich"]["step_capped"] is True
    # Якорь держит λ и никуда не упирается: лимита у него нет вовсе.
    assert moves["anchor"]["limit_binding"] is False
    assert moves["anchor"]["step_capped"] is False


# --------------------------------- бюджет кабинета растёт при запасе окупаемости


def test_budget_grows_when_lambda_has_margin():
    from sync.agent.portfolio import account_budget

    out = account_budget(current_cost=1_000_000.0, lam=2.5, target_romi=2.0,
                         room_rub=500_000.0, monthly_cap=5_000_000.0)
    assert out["budget"] == 1_200_000.0
    assert out["growth_rub"] == 200_000.0
    assert out["capped_by"] == "step"


def test_marginal_ruble_is_not_measured_against_the_contract():
    # До 28.08.2026 порог роста был target_romi × 1.2, то есть контрактная
    # двойка требовалась от ПРЕДЕЛЬНОГО рубля. Кабинет со средней 3.0 и
    # предельной 1.5 приносит втрое — растить ему можно, и старый порог 2.4
    # запрещал это на ровном месте.
    from sync.agent.portfolio import account_budget

    out = account_budget(current_cost=1_000_000.0, lam=1.5, target_romi=2.0,
                         room_rub=500_000.0, monthly_cap=5_000_000.0,
                         revenue=3_000_000.0)
    assert out["growth_rub"] == 200_000.0
    assert out["capped_by"] == "step"


def test_no_growth_when_account_is_below_contract():
    # Контракт — про СРЕДНЮЮ окупаемость. Кабинет возвращает 1.4 при
    # требуемых 2.0, и предельный рубль (1.5) контракта тоже не даёт: долив
    # уводит среднюю ещё дальше вниз. Запрет приходит от арифметики
    # контракта, а не от порога предельного рубля.
    from sync.agent.portfolio import account_budget

    out = account_budget(current_cost=1_000_000.0, lam=1.5, target_romi=2.0,
                         room_rub=500_000.0, monthly_cap=5_000_000.0,
                         revenue=1_400_000.0)
    assert out["budget"] == 1_000_000.0
    assert out["growth_rub"] == 0.0
    assert out["capped_by"] == "romi"
    assert out["romi_known"] is True


def test_contract_caps_growth_without_forbidding_it():
    # Средняя 2.1 при контракте 2.0 и предельной 1.3: долить можно ровно
    # столько, чтобы средняя села на контракт, — (2.1−2.0)·10⁶ / (2.0−1.3).
    from sync.agent.portfolio import account_budget

    out = account_budget(current_cost=1_000_000.0, lam=1.3, target_romi=2.0,
                         room_rub=500_000.0, monthly_cap=5_000_000.0,
                         revenue=2_100_000.0)
    assert out["capped_by"] == "romi"
    assert round(out["growth_rub"]) == 142_857
    # Средняя после долива села ровно на контракт, а не под него.
    assert round((2_100_000.0 + 1.3 * out["growth_rub"])
                 / (1_000_000.0 + out["growth_rub"]), 6) == 2.0


def test_contract_does_not_cap_when_marginal_beats_it():
    # λ ≥ контракта: каждый доливаемый рубль возвращает не меньше требуемого,
    # и средняя от долива РАСТЁТ. Кабинет ниже контракта (1.4) при этом
    # растить не только можно, но и нужно — это его способ дойти до
    # контракта. Ограничивать здесь нечего.
    from sync.agent.portfolio import account_budget

    out = account_budget(current_cost=1_000_000.0, lam=2.5, target_romi=2.0,
                         room_rub=500_000.0, monthly_cap=5_000_000.0,
                         revenue=1_400_000.0)
    assert out["growth_rub"] == 200_000.0
    assert out["capped_by"] == "step"


def test_unknown_revenue_leaves_the_contract_unchecked_and_says_so():
    # Выручки нет — контракт не проверяется. Запретить рост по незнанию и
    # разрешить его по незнанию врут одинаково, поэтому незнание не решает
    # ничего, а едет в отчёт полем.
    from sync.agent.portfolio import account_budget

    out = account_budget(current_cost=1_000_000.0, lam=1.5, target_romi=2.0,
                         room_rub=500_000.0, monthly_cap=5_000_000.0)
    assert out["romi_known"] is False
    assert out["capped_by"] == "step"


def test_no_growth_below_marginal_breakeven():
    # λ < 1 — предельный рубль возвращает меньше рубля выручки. Требование
    # безубыточности проверяется отдельно от запаса над целью: цель ниже
    # единицы панель настроек не даёт, но условие роста должно читаться
    # целиком, а не опираться на текущий минимум панели.
    from sync.agent.portfolio import account_budget

    out = account_budget(current_cost=1_000_000.0, lam=0.9, target_romi=0.5,
                         room_rub=500_000.0, monthly_cap=5_000_000.0)
    assert out["budget"] == 1_000_000.0
    assert out["capped_by"] == "lambda"


def test_no_growth_without_place_to_spend():
    # Окупаемость есть, а недобора нет: прибавка купит те же показы дороже.
    from sync.agent.portfolio import account_budget

    out = account_budget(current_cost=1_000_000.0, lam=3.0, target_romi=2.0,
                         room_rub=0.0, monthly_cap=5_000_000.0)
    assert out["budget"] == 1_000_000.0
    assert out["capped_by"] == "room"


def test_growth_limited_by_room():
    from sync.agent.portfolio import account_budget

    out = account_budget(current_cost=1_000_000.0, lam=3.0, target_romi=2.0,
                         room_rub=50_000.0, monthly_cap=5_000_000.0)
    assert out["growth_rub"] == 50_000.0
    assert out["capped_by"] == "room"


def test_monthly_cap_is_hard_ceiling():
    from sync.agent.portfolio import account_budget

    # Потолок назван за МЕСЯЦ, а окно солвера — 28 дней: 1 141 500 ₽ в месяц
    # это 1 050 000 ₽ за окно. Сравнивать их напрямую значило бы разрешить
    # 28 дней подряд тратить по месячному потолку — перерасход на 8,6 %.
    out = account_budget(current_cost=1_000_000.0, lam=3.0, target_romi=2.0,
                         room_rub=500_000.0, monthly_cap=1_141_500.0)
    assert out["budget"] == 1_050_000.0
    assert out["growth_rub"] == 50_000.0
    assert out["capped_by"] == "monthly_cap"


def test_monthly_cap_is_not_spent_as_if_it_were_the_window():
    # Прямое сравнение потолка с окном — та самая ошибка, которую ловит
    # пересчёт: месячный потолок, равный текущему расходу за 28 дней, роста
    # не разрешает вовсе, потому что за месяц кабинет уже потратит больше.
    from sync.agent.portfolio import account_budget

    out = account_budget(current_cost=1_000_000.0, lam=3.0, target_romi=2.0,
                         room_rub=500_000.0, monthly_cap=1_000_000.0)
    assert out["growth_rub"] == 0.0
    assert out["capped_by"] == "monthly_cap"


def test_cap_below_current_spend_does_not_shrink_the_account():
    # Потолок ниже факта — это команда сокращать общую сумму, а сокращения
    # по кабинету агент не делает: он растит или держит. Сумма остаётся, а
    # упор в потолок виден в отчёте.
    from sync.agent.portfolio import account_budget

    out = account_budget(current_cost=1_000_000.0, lam=3.0, target_romi=2.0,
                         room_rub=500_000.0, monthly_cap=800_000.0)
    assert out["budget"] == 1_000_000.0
    assert out["growth_rub"] == 0.0
    assert out["capped_by"] == "monthly_cap"


def test_without_cap_growth_is_proposed_not_applied():
    # Потолок не задан — сумма не меняется, но предложение посчитано: общий
    # бюджет это деньги владельца, и цифру ставит он.
    from sync.agent.portfolio import account_budget

    out = account_budget(current_cost=1_000_000.0, lam=3.0, target_romi=2.0,
                         room_rub=500_000.0, monthly_cap=None)
    assert out["budget"] == 1_000_000.0
    assert out["growth_rub"] == 0.0
    assert out["proposed_growth_rub"] == 200_000.0


def test_solver_keeps_the_account_sum_without_room():
    # Запас не передан — расти некуда, и инвариант «Σ целевых = факту»
    # остаётся ровно тем, чем был до задачи 11.
    saturation, ladder = _inputs()
    section = portfolio_targets(saturation, ladder, {"1": "acc", "2": "acc"},
                                monthly_cap_rub=5_000_000.0)
    acc = section["accounts"]["acc"]
    assert acc["budget_28d"] == 200_000.0
    assert acc["growth_rub"] == 0.0
    assert acc["growth_capped_by"] == "room"


def test_solver_grows_the_account_within_the_step():
    saturation = {"1": _curve()}
    ladder = {"1": _ladder()}
    section = portfolio_targets(saturation, ladder, {"1": "acc"},
                                room_rub_by_login={"acc": 50_000.0},
                                monthly_cap_rub=5_000_000.0)
    acc = section["accounts"]["acc"]
    # λ = 10 000 / 1250 = 8 — запас над целью 1.0 огромный.
    assert acc["lambda_breakeven"] is True
    assert acc["cost_28d"] == 100_000.0
    assert acc["budget_28d"] == 120_000.0
    assert acc["growth_rub"] == 20_000.0
    assert acc["growth_capped_by"] == "step"
    assert acc["deferred_growth_rub"] == 0.0
    # Инвариант тот же, только бюджет теперь больше факта.
    assert abs(acc["sum_residual"]) < 1.0
    assert abs(acc["target_sum_28d"] - 120_000.0) < 1.0


def test_growth_beyond_step_caps_is_deferred_not_smeared():
    # β ≥ 1: шаг такой кампании — умеренные ×1.15, и прибавку +20 %
    # разложить некуда. Остаток не размазывается, а снимается с бюджета.
    saturation = {"1": _curve(beta=1.1)}
    ladder = {"1": _ladder()}
    section = portfolio_targets(saturation, ladder, {"1": "acc"},
                                room_rub_by_login={"acc": 50_000.0},
                                monthly_cap_rub=5_000_000.0)
    acc = section["accounts"]["acc"]
    assert acc["budget_28d"] == 115_000.0
    assert acc["growth_rub"] == 15_000.0
    assert acc["deferred_growth_rub"] == 5_000.0
    # Инвариант «Σ целевых = бюджету» держится: невязка снята, а не спрятана.
    assert abs(acc["sum_residual"]) < 1.0
    # Порог остался порогом текущего расхода: на плато капов ограничение
    # суммы его не задаёт, и бинарный поиск ушёл бы в ноль — а нулевой λ
    # объявил бы кабинет убыточным и раздул бы уверенность каждого сдвига.
    assert acc["lambda"] > 1.0
    assert acc["lambda_breakeven"] is True


def test_growth_is_capped_by_the_monthly_plan_of_the_owner():
    saturation = {"1": _curve()}
    ladder = {"1": _ladder()}
    # 119 585,71 ₽ в месяц = 110 000 ₽ за окно в 28 дней.
    section = portfolio_targets(saturation, ladder, {"1": "acc"},
                                room_rub_by_login={"acc": 50_000.0},
                                monthly_cap_rub=119_585.71)
    acc = section["accounts"]["acc"]
    assert abs(acc["budget_28d"] - 110_000.0) < 0.5
    assert acc["growth_capped_by"] == "monthly_cap"


def test_without_the_monthly_plan_growth_is_only_proposed():
    saturation = {"1": _curve()}
    ladder = {"1": _ladder()}
    section = portfolio_targets(saturation, ladder, {"1": "acc"},
                                room_rub_by_login={"acc": 50_000.0})
    acc = section["accounts"]["acc"]
    assert acc["budget_28d"] == 100_000.0
    assert acc["growth_rub"] == 0.0
    assert acc["proposed_growth_rub"] == 20_000.0


def test_target_romi_is_checked_against_the_account_average():
    # Кабинет фикстуры возвращает 10.0 (выручка 1 млн на расход 100 тыс).
    # Контракт 8.0 он выполняет — рост разрешён; контракт 12.0 нарушает —
    # рост запрещён, и причина названа своим именем, а не «lambda».
    saturation = {"1": _curve()}
    ladder = {"1": _ladder()}

    ok = portfolio_targets(saturation, ladder, {"1": "acc"},
                           target_romi=8.0,
                           room_rub_by_login={"acc": 50_000.0},
                           monthly_cap_rub=5_000_000.0)["accounts"]["acc"]
    assert ok["account_romi"] == 10.0
    assert ok["growth_rub"] > 0.0

    over = portfolio_targets(saturation, ladder, {"1": "acc"},
                             target_romi=12.0,
                             room_rub_by_login={"acc": 50_000.0},
                             monthly_cap_rub=5_000_000.0)["accounts"]["acc"]
    assert over["budget_28d"] == 100_000.0
    assert over["growth_capped_by"] == "romi"
    assert over["target_romi"] == 12.0


def test_bias_multiplier_is_one_without_history():
    from sync.agent.portfolio import bias_multiplier

    assert bias_multiplier(None, "up") == 1.0
    assert bias_multiplier({}, "up") == 1.0
    # Есть история другой стороны — своей стороне это ничего не говорит.
    assert bias_multiplier({"budget.set:down": {"shrunk_ratio": 0.5, "n": 9}},
                           "up") == 1.0


def test_bias_multiplier_takes_the_shrunk_ratio_of_its_side():
    from sync.agent.portfolio import bias_multiplier

    bias = {"budget.set:up": {"ratio": 0.4, "shrunk_ratio": 0.7, "n": 12},
            "budget.set:down": {"ratio": 1.9, "shrunk_ratio": 1.4, "n": 5}}
    # Берётся усаженное отношение, а не сырая медиана: на десятке наблюдений
    # медиана отношений — это шум, помноженный на план.
    assert bias_multiplier(bias, "up") == 0.7
    assert bias_multiplier(bias, "down") == 1.4


def test_bias_multiplier_prefers_the_longer_history_of_the_two_levers():
    from sync.agent.portfolio import bias_multiplier

    # Бюджетный сдвиг исполняется двумя рычагами (недельный лимит и дневной
    # бюджет), и какой достанется кампании, решает писатель по её стратегии.
    # Солвер берёт того, чья история длиннее.
    bias = {"budget.set:up": {"shrunk_ratio": 0.6, "n": 3},
            "budget.set_daily:up": {"shrunk_ratio": 0.9, "n": 30}}
    assert bias_multiplier(bias, "up") == 0.9


def test_wild_bias_is_clamped():
    from sync.agent.portfolio import (BIAS_MULTIPLIER_MAX, BIAS_MULTIPLIER_MIN,
                                      bias_multiplier)

    # За полосой это уже не калибровка, а вопрос «почему модель промахнулась
    # в разы»: разбирается руками по forecast_bias, а не множится на план.
    assert bias_multiplier({"budget.set:up": {"shrunk_ratio": 9.0, "n": 40}},
                           "up") == BIAS_MULTIPLIER_MAX
    assert bias_multiplier({"budget.set:up": {"shrunk_ratio": 0.01, "n": 40}},
                           "up") == BIAS_MULTIPLIER_MIN


def test_calibration_corrects_the_expectation_but_not_the_raw_number():
    saturation = {"1": _curve()}
    ladder = {"1": _ladder()}
    plain = portfolio_targets(saturation, ladder, {"1": "acc"})
    calibrated = portfolio_targets(
        saturation, ladder, {"1": "acc"},
        forecast_bias={"budget.set:up": {"shrunk_ratio": 0.5, "n": 40},
                       "budget.set:down": {"shrunk_ratio": 0.5, "n": 40}})

    a = plain["accounts"]["acc"]["moves"]["1"]
    b = calibrated["accounts"]["acc"]["moves"]["1"]
    # Цели не двигаются: сколько тратить, решает кривая насыщения, а петля
    # обучения знает лишь то, насколько сбывались обещания.
    assert a["target_28d"] == b["target_28d"]
    # Сырое ожидание остаётся сырым — им меряется сама поправка. Подмени его,
    # и петля мерила бы себя: любое смещение сошлось бы к единице.
    assert a["expected_leads_delta"] == b["expected_leads_delta"]
    assert b["forecast_bias_ratio"] == 0.5
    assert b["expected_leads_delta_calibrated"] == round(
        b["expected_leads_delta"] * 0.5, 1)
    assert a["forecast_bias_ratio"] == 1.0
    assert a["expected_leads_delta_calibrated"] == a["expected_leads_delta"]


def test_computed_rows_carry_both_expectations():
    saturation = {"1": _curve()}
    ladder = {"1": _ladder()}
    section = portfolio_targets(
        saturation, ladder, {"1": "acc"},
        forecast_bias={"budget.set:up": {"shrunk_ratio": 0.5, "n": 40},
                       "budget.set:down": {"shrunk_ratio": 0.5, "n": 40}})
    rows = computed_rows(section)["1"]
    by_key = {r["setting_key"]: r for r in rows}
    assert "expected_leads_delta" in by_key
    calibrated = by_key["expected_leads_delta_calibrated"]
    assert calibrated["value"] == round(
        by_key["expected_leads_delta"]["value"] * 0.5, 1)
    # Множитель едет рядом: иначе разницу двух строк пришлось бы выводить
    # делением и гадать, поправка это или другая модель.
    assert calibrated["raw_value"] == 0.5
