# -*- coding: utf-8 -*-
"""
tests/test_beta_readiness_lanes.py — проверка готовности видит полосы.

Первый боевой прогон беты стартует с полосами 1–2 на ступени 1 и остальными
в тени. Ступень, выданная ПОЛОМ полосы (lanes.default_step_of), от
заработанной снаружи неотличима — оба раза это просто число, — и без
отдельного правила проверка готовности сказала бы «готов» про рычаг, о
котором не известно ничего.

БД не требуется: проверка чистая, на вход идёт уже собранный отчёт.
"""

import probe_beta_readiness as probe
from sync.agent.writer import lanes


def _report(lanes_section):
    """Отчёт, у которого всё остальное заведомо в порядке.

    Иначе тест про полосы падал бы на соседних блокерах и доказывал бы не то,
    что проверяет.
    """
    return {
        "schema": {"tables_missing": [], "registry_columns_missing": [],
                   "action_columns_missing": []},
        "config": {"protocol_mismatch": {}},
        "data_gate": {"status": "GREEN"},
        "runs": {stage: {"runs_14d": 3, "verdict_is_null": False}
                 for stage in ("e0", "e1", "watchdog")},
        "actions": {"live_writes_total": 5},
        "bets": {"error": None, "computed_exploration_rub": [],
                 "exploration_actions": [], "action_kinds_30d": {}},
        "holdout": {"campaigns": 7},
        "write_rights": [{"account": "edu-vuz", "verdict": "WRITE_OK"}],
        "lanes": lanes_section,
    }


def _lane(step, source, closed=0.0, improved=0.0):
    return {"step": step, "source": source, "closed": closed,
            "improved": improved,
            "hit_rate": (improved / closed) if closed else None,
            "journal_unavailable": None}


def test_a_lane_without_a_track_record_above_shadow_blocks_the_beta():
    # Пол полосы — вход в лестницу, и на обычном такте он законен. На первом
    # боевом прогоне это деньги под рычагом, о котором не известно ничего.
    out = probe.verdict(_report({"launch": _lane(1, lanes.STEP_FLOOR)}))

    assert out["ready"] is False
    assert any("launch" in b and "тени" in b for b in out["blockers"])


def test_the_shadow_lane_is_not_a_blocker():
    out = probe.verdict(_report({"launch": _lane(0, lanes.STEP_SHADOW)}))

    assert out["ready"] is True
    assert out["blockers"] == []


def test_an_earned_step_is_not_a_blocker():
    # Двенадцать закрытых наблюдений — это ровно то, ради чего лестница и
    # заведена: свобода, заработанная послужным списком.
    out = probe.verdict(
        _report({"tuning": _lane(1, lanes.STEP_EARNED, closed=12, improved=8)}))

    assert out["ready"] is True


def test_a_human_raised_lane_is_a_warning_and_not_a_blocker():
    # Решение человека проверка не отменяет: отменять его за него нечем. Но и
    # молчать нельзя — доля улучшений у такой полосы неизвестна.
    out = probe.verdict(_report({"launch": _lane(1, lanes.STEP_HUMAN)}))

    assert out["ready"] is True
    assert any("launch" in w and "человек" in w for w in out["warnings"])


def test_an_unavailable_journal_is_a_blocker_not_a_pass():
    # «Не смогли посмотреть» — не «посмотрели и всё хорошо»: без журнала
    # ступень посчитать нечем, а лестница молча вернула бы пол.
    slot = _lane(1, lanes.STEP_FLOOR)
    slot["journal_unavailable"] = "OperationalError: connection refused"

    out = probe.verdict(_report({"tuning": slot}))

    assert out["ready"] is False
    assert any("журнал" in b for b in out["blockers"])
