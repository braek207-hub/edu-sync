# -*- coding: utf-8 -*-
"""
tests/test_agent_value.py — перевод замеров агента в рубли.

Два слоя уже посчитаны: эффект такта против заповедника (DiD, sync/agent/
tact_effect.py) и исход собственного действия против ожидания (журнал,
writer/db.closed_actions). Ни один из них не отвечает на вопрос «сколько
агент принёс за месяц» — и именно этот вопрос задаёт владелец.

Главное утверждение файла: НЕИЗМЕРЕННОЕ НЕ РАВНО НУЛЮ. Такт с вердиктом
inconclusive не «не дал выгоды» — про него нельзя утверждать ничего, и
рублёвый ноль рядом с измеренными тактами читался бы как провал. Поэтому у
каждой суммы едет доля неизмеренного, а у тактов — интервал.
"""

import sync.agent.value as value
from sync.agent.writer.lanes import LANE_ALLOCATION, LANE_HYGIENE


# --------------------------------------------------------------------- такты


def test_value_converts_did_into_rubles():
    tact = {"verdict": "improved", "did": -0.10, "cost_treated_after": 1_000_000.0}
    out = value.tact_value(tact)
    assert out["saved_rub"] == 100_000.0 and out["measured"] is True


def test_inconclusive_is_unmeasured_not_zero():
    out = value.tact_value({"verdict": "inconclusive", "did": -0.03,
                            "cost_treated_after": 1e6})
    assert out["saved_rub"] == 0.0 and out["measured"] is False


def test_tact_value_reads_cost_from_the_measure_output():
    """Форма tact_effect.measure(): расход обработанных лежит в treated.cost.

    Плоский cost_treated_after — форма плана; из чёрного ящика приезжает
    вторая. Нормализация одна на оба входа, иначе замер сторожа молча дал бы
    нулевой расход и нулевую выгоду при живом did.
    """
    out = value.tact_value({"verdict": "improved", "did": -0.2,
                            "treated": {"cost": 500_000.0, "leads": 40}})
    assert out["saved_rub"] == 100_000.0


def test_worsened_tact_costs_money_with_a_positive_did():
    out = value.tact_value({"verdict": "worsened", "did": 0.15,
                            "cost_treated_after": 200_000.0})
    assert out["saved_rub"] == -30_000.0 and out["measured"] is True


def test_interval_of_an_inconclusive_tact_includes_zero():
    """Честная граница: у неизмеренного такта интервал есть и включает ноль."""
    out = value.tact_value({"verdict": "inconclusive", "did": -0.03,
                            "ci": (-0.20, 0.14), "cost_treated_after": 1e6})
    assert out["interval_rub"] == (-140_000.0, 200_000.0)
    assert out["measured"] is False


def test_interval_is_ordered_low_first():
    out = value.tact_value({"verdict": "improved", "did": -0.10,
                            "ci": (-0.18, -0.02), "cost_treated_after": 1e6})
    low, high = out["interval_rub"]
    assert low <= high and (low, high) == (20_000.0, 180_000.0)


def test_unknown_tact_has_neither_rubles_nor_interval():
    out = value.tact_value({"verdict": "unknown", "did": None, "ci": None,
                            "treated": {}})
    assert out["saved_rub"] == 0.0
    assert out["measured"] is False
    assert out["interval_rub"] is None
    assert out["verdict"] == "unknown"


def test_a_good_verdict_without_spend_is_not_a_measured_zero():
    """Расхода нет — переводить долю в рубли нечем, и это не «выгоды ноль».

    Ноль рублей при measured=True утверждал бы, что такт ничего не дал, тогда
    как утверждать нечего вовсе: множителя не существует. Интервала по той же
    причине тоже нет — (0, 0) читался бы как «точно ноль».
    """
    out = value.tact_value({"verdict": "improved", "did": -0.3,
                            "ci": (-0.5, -0.1), "treated": {"cost": 0.0}})
    assert out["saved_rub"] == 0.0
    assert out["measured"] is False
    assert out["interval_rub"] is None


# ----------------------------------------------------------------- действия


def _action(kind, observed=None, rub_delta=None):
    row = {"action_kind": kind, "object_id": "111", "applied_on": "2026-08-01",
           "closing_verdict": "hit", "money_verdict": None,
           "expected_leads_delta": 1.0, "observed_leads_delta": observed}
    if rub_delta is not None:
        row["expected_rub_delta"] = rub_delta
    return row


def test_action_value_turns_observed_leads_into_rubles():
    out = value.action_value(_action("budget.set", observed=12.0), 3_000.0)
    assert out["earned_rub"] == 36_000.0
    assert out["measured"] is True
    assert out["lane"] == LANE_ALLOCATION


def test_action_without_a_lead_price_is_unmeasured_not_worthless():
    """Цены лида нет — заработок не выдумывается, а объявляется неизмеренным."""
    out = value.action_value(_action("budget.set", observed=12.0), None)
    assert out["earned_rub"] == 0.0 and out["measured"] is False


def test_action_without_an_observed_delta_is_unmeasured():
    out = value.action_value(_action("budget.set", observed=None), 3_000.0)
    assert out["earned_rub"] == 0.0 and out["measured"] is False


def test_hygiene_cut_is_planned_and_says_so():
    """Сторож при закрытии кладёт в журнал только вердикт и дельту лидов.

    Фактического расхода до/после в edu_agent_actions нет ни в одной колонке
    (mark_observation_closed пишет observation_verdict и observed_leads_delta),
    поэтому вырезанные рубли берутся из ОБЕЩАНИЯ рычага — и печатаются с
    basis='planned', а не выдаются за измеренные.
    """
    out = value.action_value(_action("negative.add", observed=0.0,
                                     rub_delta=-42_000.0), 3_000.0)
    assert out["lane"] == LANE_HYGIENE
    assert out["cut_rub"] == 42_000.0
    assert out["basis"] == "planned"


def test_hygiene_without_a_promise_cuts_nothing_and_names_no_basis():
    out = value.action_value(_action("negative.add", observed=0.0), 3_000.0)
    assert out["cut_rub"] == 0.0 and out["basis"] is None


def test_only_hygiene_counts_as_a_cut():
    """Сдвиг лимита переносит деньги, а не снимает их с кабинета."""
    out = value.action_value(_action("budget.set", observed=1.0,
                                     rub_delta=-50_000.0), 3_000.0)
    assert out["cut_rub"] == 0.0 and out["basis"] is None


def test_unknown_action_kind_gets_an_unknown_lane():
    out = value.action_value(_action("something.new", observed=1.0), 100.0)
    assert out["lane"] == "unknown"


# ------------------------------------------------------ такты из чёрного ящика


def _effect(day, verdict="improved", did=-0.1, cost=1e6):
    return {"tact_date": day, "verdict": verdict, "did": did,
            "ci": (-0.2, -0.02), "treated": {"cost": cost}}


def _watchdog_report(day, account="acc-1", verdict="improved", did=-0.1,
                     cost=1e6):
    return {"today": day,
            "accounts": [{"account": account,
                          "tact_effect": _effect(day, verdict, did, cost)}]}


def test_tacts_are_read_from_accounts_not_from_the_report_root():
    """Замер такта лежит в отчёте сторожа внутри кабинета, а не в корне.

    На верхнем уровне ключа tact_effect нет вовсе (agent_e1_watchdog.main).
    Чтение из корня вернуло бы пусто МОЛЧА, и секция выгоды печатала бы нули
    при живых замерах.
    """
    tacts = value.tacts_from_reports([_watchdog_report("2026-08-10")])
    assert len(tacts) == 1
    assert tacts[0]["did"] == -0.1
    assert tacts[0]["account"] == "acc-1"


def test_two_runs_of_one_day_are_one_tact():
    """Дубль по дню схлопывается: побеждает последний прогон."""
    tacts = value.tacts_from_reports([
        _watchdog_report("2026-08-10", did=-0.1),
        _watchdog_report("2026-08-10", did=-0.3),
    ])
    assert [t["did"] for t in tacts] == [-0.3]


def test_different_accounts_of_one_day_are_different_tacts():
    """Кабинеты меряются порознь: у каждого свои обработанные и свой расход.

    Схлопывать их по одной дате значило бы выбросить замер соседнего кабинета
    и занизить выгоду ровно на его величину.
    """
    tacts = value.tacts_from_reports([
        {"accounts": [{"account": "acc-1", "tact_effect": _effect("2026-08-10")},
                      {"account": "acc-2", "tact_effect": _effect("2026-08-10")}]},
    ])
    assert len(tacts) == 2


def test_a_report_without_a_measure_yields_no_tact():
    assert value.tacts_from_reports([{"accounts": [{"account": "acc-1"}]}]) == []


# ------------------------------------------------------------------- период


def test_empty_period_is_zeroes_and_not_a_full_share_of_unmeasured():
    out = value.period_value([], [], {})
    assert out["saved_rub"] == 0.0
    assert out["n_tacts"] == 0 and out["n_actions"] == 0
    assert out["unmeasured_share"] == 0.0
    assert out["did_interval_rub"] is None
    assert out["by_lane"] == {}


def test_period_counts_unmeasured_without_zeroing_the_sum():
    tacts = [{"verdict": "improved", "did": -0.1, "cost_treated_after": 1e6},
             {"verdict": "inconclusive", "did": -0.05, "cost_treated_after": 1e6},
             {"verdict": "unknown", "did": None}]
    actions = [_action("budget.set", observed=10.0),
               _action("budget.set", observed=None)]
    out = value.period_value(tacts, actions, {"111": 2_000.0})

    assert out["saved_rub"] == 100_000.0
    assert (out["n_tacts"], out["n_tacts_measured"]) == (3, 1)
    assert (out["n_actions"], out["n_actions_measured"]) == (2, 1)
    assert out["earned_rub"] == 20_000.0
    # Три неизмеренных из пяти наблюдений — и это печатается рядом с суммой.
    assert out["unmeasured_share"] == 0.6


def test_period_interval_sums_componentwise():
    tacts = [{"verdict": "improved", "did": -0.1, "ci": (-0.2, -0.02),
              "cost_treated_after": 1e6},
             {"verdict": "inconclusive", "did": 0.0, "ci": (-0.1, 0.1),
              "cost_treated_after": 1e6}]
    out = value.period_value(tacts, [], {})
    # (20 000 + −100 000, 200 000 + 100 000)
    assert out["did_interval_rub"] == [-80_000.0, 300_000.0]


def test_period_splits_money_by_lane():
    actions = [_action("budget.set", observed=10.0),
               _action("negative.add", observed=0.0, rub_delta=-30_000.0),
               _action("negative.add", observed=0.0, rub_delta=-12_000.0)]
    out = value.period_value(tacts=[], actions=actions,
                             value_per_lead_by_campaign={"111": 2_000.0})

    assert out["by_lane"][LANE_ALLOCATION] == {"earned_rub": 20_000.0,
                                               "cut_rub": 0.0, "n": 1}
    assert out["by_lane"][LANE_HYGIENE] == {"earned_rub": 0.0,
                                            "cut_rub": 42_000.0, "n": 2}
    assert out["cut_rub"] == 42_000.0


def test_lead_price_is_matched_by_object_id():
    """Цена лида ищется по объекту действия, а не берётся первой попавшейся."""
    actions = [_action("budget.set", observed=10.0)]
    out = value.period_value([], actions, {"999": 5_000.0})
    assert out["earned_rub"] == 0.0 and out["n_actions_measured"] == 0
