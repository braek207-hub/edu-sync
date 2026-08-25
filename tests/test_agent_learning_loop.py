# -*- coding: utf-8 -*-
"""
tests/test_agent_learning_loop.py — петля обучения: агент учится на СВОИХ
закрытых действиях, а не только на чужих скачках бюджета в фактах.

Данные — литералы: журнал сюда приходит уже прочитанным
(writer_db.closed_actions), и модуль сам никуда не ходит.
"""

from sync.agent.learning_loop import (
    BIAS_PRIOR_N,
    MIN_EXPECTED_LEADS,
    forecast_bias,
    track_record,
)


def _action(kind, verdict, money=None, expected=None, observed=None,
            verdict_key="closing_verdict"):
    return {"action_kind": kind, verdict_key: verdict, "money_verdict": money,
            "expected_leads_delta": expected, "observed_leads_delta": observed}


# ------------------- послужной список по рычагам


def test_track_record_counts_by_lever():
    actions = [_action("budget.set", "improved"), _action("budget.set", "worsened"),
               _action("bidmodifier.set", "improved")]
    out = track_record(actions)
    assert out["budget.set"]["closed"] == 2
    assert out["budget.set"]["improved"] == 1
    assert out["budget.set"]["hit_rate"] == 0.5
    assert out["bidmodifier.set"]["hit_rate"] == 1.0


def test_money_contradiction_counted_separately():
    # Заявка подешевела, оплата подорожала — успех по заявкам, промах по деньгам.
    actions = [_action("tcpa.set", "improved", money="worsened"),
               _action("tcpa.set", "improved", money="improved")]
    out = track_record(actions)
    assert out["tcpa.set"]["money_confirmed"] == 1
    assert out["tcpa.set"]["money_contradicted"] == 1


def test_unknown_verdicts_do_not_count_as_success():
    actions = [_action("budget.set", "unknown"), _action("budget.set", "improved")]
    out = track_record(actions)
    assert out["budget.set"]["closed"] == 1
    assert out["budget.set"]["hit_rate"] == 1.0


def test_inconclusive_is_a_closed_observation_but_not_a_hit():
    # Шкала журнала — improved/worsened/inconclusive/unknown
    # (writer/rollback.outcome_verdict). «Эффект неотличим от нуля» — это
    # закрытое наблюдение, но не попадание: считать его успехом значит
    # вернуть winner's curse, ради которого шкалу и делали непрерывной.
    out = track_record([_action("budget.set", "inconclusive"),
                        _action("budget.set", "improved")])
    assert out["budget.set"]["closed"] == 2
    assert out["budget.set"]["inconclusive"] == 1
    assert out["budget.set"]["hit_rate"] == 0.5


def test_raw_journal_row_is_read_by_its_real_column_name():
    # Ловушка: в БД колонка называется observation_verdict, а closing_verdict —
    # это функция сторожа, которая её заполняет. Строка, прочитанная без
    # алиаса, обязана считаться так же, иначе петля молча получит пустоту.
    out = track_record([_action("budget.set", "improved",
                                verdict_key="observation_verdict")])
    assert out["budget.set"]["hit_rate"] == 1.0


def test_hit_rate_is_printed_separately_for_growth_and_for_cuts():
    # Мера успеха однобока: сокращение почти всегда «дешевеет», доливка почти
    # всегда «дорожает». Общий hit_rate по рычагу смешал бы их и выдал бы
    # свободу резакам — поэтому направления считаются раздельно.
    actions = [_action("budget.set", "improved", expected=10.0, observed=8.0),
               _action("budget.set", "worsened", expected=10.0, observed=1.0),
               _action("budget.set", "improved", expected=-10.0, observed=-9.0)]
    out = track_record(actions)["budget.set"]
    assert out["closed_up"] == 2
    assert out["hit_rate_up"] == 0.5
    assert out["closed_down"] == 1
    assert out["hit_rate_down"] == 1.0


def test_direction_unknown_stays_out_of_the_split_but_counts_overall():
    out = track_record([_action("negative.add", "improved")])["negative.add"]
    assert out["closed"] == 1
    assert out["closed_up"] == 0 and out["closed_down"] == 0
    assert out["hit_rate_up"] is None and out["hit_rate_down"] is None


# ------------------- смещение прогноза


def test_forecast_bias_is_median_of_fact_over_expectation():
    actions = [_action("budget.set", "improved", expected=10.0, observed=5.0),
               _action("budget.set", "improved", expected=10.0, observed=6.0),
               _action("budget.set", "improved", expected=10.0, observed=4.0)]
    out = forecast_bias(actions)
    assert out["budget.set:up"]["ratio"] == 0.5
    assert out["budget.set:up"]["n"] == 3


def test_bias_keys_keep_growth_and_cuts_apart():
    # Модель может завышать эффект доливки и занижать эффект срезания. Одно
    # усреднённое число по budget.set спрятало бы обе ошибки друг за другом.
    actions = [_action("budget.set", "improved", expected=10.0, observed=5.0),
               _action("budget.set", "improved", expected=-10.0, observed=-20.0)]
    out = forecast_bias(actions)
    assert out["budget.set:up"]["ratio"] == 0.5
    assert out["budget.set:down"]["ratio"] == 2.0


def test_bias_is_shrunk_towards_one_on_thin_evidence():
    # Три наблюдения не повод верить, что модель завышает вдвое: усадка к 1.0
    # по объёму (тот же приём, что эмпирический Байес в history.combine).
    actions = [_action("budget.set", "improved", expected=10.0, observed=5.0)] * 3
    shrunk = forecast_bias(actions)["budget.set:up"]["shrunk_ratio"]
    assert 0.5 < shrunk < 1.0
    assert round(shrunk, 4) == round((0.5 * 3 + 1.0 * BIAS_PRIOR_N) / (3 + BIAS_PRIOR_N), 4)


def test_zero_expectation_is_skipped_not_infinite():
    actions = [_action("budget.set", "improved", expected=0.0, observed=5.0)]
    assert forecast_bias(actions) == {}


def test_expectation_below_one_lead_is_skipped():
    # Отношение к почти нулю даёт сотни, и даже медиана поплывёт, если таких
    # действий наберётся половина.
    tiny = MIN_EXPECTED_LEADS / 2
    assert forecast_bias([_action("budget.set", "improved",
                                  expected=tiny, observed=5.0)]) == {}
    assert forecast_bias([_action("budget.set", "improved",
                                  expected=-tiny, observed=-5.0)]) == {}


def test_missing_fact_is_not_a_zero_effect():
    # observed_leads_delta = NULL означает «не измерено»: у действия не было
    # темпа базы. Прочитать это как «эффекта не было» значило бы записать
    # прогнозу промах, которого никто не наблюдал.
    assert forecast_bias([_action("budget.set", "improved", expected=10.0,
                                  observed=None)]) == {}


def test_measured_zero_effect_is_kept():
    out = forecast_bias([_action("budget.set", "improved", expected=10.0,
                                 observed=0.0)])
    assert out["budget.set:up"]["ratio"] == 0.0


def test_empty_journal_gives_empty_answer():
    assert track_record([]) == {}
    assert forecast_bias([]) == {}


# ------------------- выборка журнала: имена колонок, а не имена функций


def test_closed_actions_reads_columns_that_really_exist():
    """Ловушка: SQL, написанный по имени функции сторожа, вернёт пустоту молча.

    В журнале колонка называется observation_verdict; closing_verdict — это
    функция, которая её заполняет. Отказ был бы бесшумным: петля просто
    перестала бы учиться, не уронив ни одного прогона.
    """
    from sync.agent.writer.db import CLOSED_ACTIONS_SQL, WRITER_DDL

    ddl = "\n".join(WRITER_DDL)
    for column in ("action_kind", "applied_at", "payload", "money_verdict",
                   "observation_verdict", "observed_leads_delta"):
        assert column in ddl, column
    assert "closing_verdict" not in ddl
    assert "observation_verdict AS closing_verdict" in CLOSED_ACTIONS_SQL
