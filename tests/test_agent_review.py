# -*- coding: utf-8 -*-
"""Недельный разбор беты: находки, а не счётчики.

Чёрный ящик копит факты. Дефект подхода в них не виден по одному дню — он
виден закономерностью: одна и та же стена, один и тот же спор рычагов, один
и тот же объект, который человек возвращает руками.
"""

from sync.agent import rejects, review


def _reject(run_id="r1", reason="cooldown", object_id="111",
            kind="MOBILE_ADJUSTMENT", key="MOBILE", cost=100.0):
    return {"run_id": run_id, "stage": "e1", "account": "acc",
            "object_id": object_id, "kind": kind, "key": key,
            "reason": reason, "cost_rub": cost, "risk_rub": 10.0}


def _run(stage="e1", report=None, run_id="r1"):
    return {"run_id": run_id, "stage": stage, "mode": "apply",
            "report": report or {}}


def test_one_refusal_is_not_a_finding():
    assert review.walls([_reject()]) == []


def test_the_same_wall_in_three_runs_is_a_finding():
    # Один отказ — случайность дня. Три подряд означают, что агент каждый
    # день тратит расчёт на действие, которое не пройдёт никогда.
    rows = [_reject(run_id=f"r{i}") for i in range(3)]

    found = review.walls(rows)

    assert len(found) == 1
    assert found[0]["code"] == review.WALL
    assert found[0]["evidence"]["runs"] == 3


def test_three_refusals_inside_one_run_are_not_history():
    rows = [_reject(run_id="r1") for _ in range(3)]

    assert review.walls(rows) == []


def test_working_limiters_are_not_complaints():
    # Это работающие ограничители: они обязаны срабатывать каждый день, и
    # жаловаться на них — жаловаться на замысел.
    #
    # Убрать отсюда lane_limit или proposal — и первый же недельный разбор
    # после снятия лимита действий превратится в свалку: полоса отказывает
    # сотням кандидатов каждый прогон, предложение не применяется НИКОГДА по
    # построению, и оба дают «стену» на третий день по каждому объекту.
    # Настоящие находки (units_low, конфликты) в этой куче не найдёт никто, а
    # с --fail-on-high разбор станет вечно красным.
    for reason in (rejects.BUDGET, rejects.LANE_LIMIT, rejects.PROPOSAL,
                   rejects.RUN_CAP):
        rows = [_reject(run_id=f"r{i}", reason=reason) for i in range(5)]
        assert review.walls(rows) == [], reason


def test_expected_reasons_are_real_reason_codes():
    # Перечень строится из констант rejects, а не из литералов: опечатка в
    # строке не падает, она молча возвращает жалобу на работающий
    # ограничитель — и разбор снова тонет в шуме.
    assert review.EXPECTED_REASONS <= rejects.READABLE_REASONS


def test_running_out_of_direct_units_stays_a_finding():
    # Обратная граница: исчерпание баллов кабинета замыслом НЕ является.
    # Хвост такта, который не уехал третий прогон подряд, — это находка, и
    # расширение EXPECTED_REASONS не имеет права её проглотить.
    rows = [_reject(run_id=f"r{i}", reason=rejects.UNITS_LOW) for i in range(3)]

    assert len(review.walls(rows)) == 1


def test_expensive_object_outranks_a_sleeping_one():
    cheap = [_reject(run_id=f"r{i}", object_id="222", cost=10.0) for i in range(3)]
    rich = [_reject(run_id=f"r{i}", object_id="111", cost=5000.0) for i in range(3)]

    found = {f["evidence"]["object_id"]: f["severity"] for f in review.walls(cheap + rich)}

    assert found["111"] == "high"
    assert found["222"] == "medium"


def test_silent_stage_is_found_by_absence():
    # Молчащий крон выглядит ровно как «всё хорошо»: ни ошибки, ни строки.
    found = review.silent_stages([_run(stage="e1")], ("e0", "e1", "drift"))

    assert {f["subject"] for f in found} == {"e0", "drift"}


def test_recurring_conflict_needs_more_than_one_run():
    one = [_run(report={"accounts": [{"conflicts": {"conflict_opposing_levers": 2}}]})]
    two = one + [_run(run_id="r2",
                      report={"accounts": [{"conflicts": {"conflict_opposing_levers": 1}}]})]

    assert review.conflicts_seen(one) == []
    assert review.conflicts_seen(two)[0]["evidence"] == {"runs": 2, "actions": 3}


def test_hand_rollback_is_the_heaviest_finding():
    runs = [_run(stage="drift", report={"rows": [
        {"verdict": "reverted", "account": "acc", "object_id": "111",
         "direct_type": "WEEKLY_SPEND_LIMIT", "key": "search"},
        {"verdict": "match", "object_id": "222"},
    ]})]

    found = review.hand_rollbacks(runs)

    assert len(found) == 1
    assert found[0]["severity"] == "high"
    assert found[0]["code"] == review.HAND_ROLLBACK


def test_unverified_kinds_are_reported_as_a_blind_spot():
    runs = [_run(stage="drift", report={"unverified_kinds": {"EXCLUDED_SITES": 4}})]

    found = review.unverified(runs)

    assert found[0]["subject"] == "EXCLUDED_SITES"
    assert found[0]["evidence"]["actions"] == 4


def test_failed_blackbox_write_means_a_missing_run():
    runs = [_run(report={"blackbox": {"saved": False, "error": "OperationalError: нет связи"}})]

    found = review.blind_writes(runs)

    assert found[0]["code"] == review.BLIND_WRITE


def test_findings_are_sorted_by_weight():
    runs = [_run(stage="drift", report={
        "unverified_kinds": {"EXCLUDED_SITES": 1},
        "rows": [{"verdict": "reverted", "object_id": "111"}]})]

    out = review.review(runs, [], expected_stages=("drift",))

    assert [f["severity"] for f in out["findings"]] == ["high", "low"]
    assert out["by_severity"] == {"high": 1, "medium": 0, "low": 1}


def test_empty_period_is_not_an_error():
    out = review.review([], [], expected_stages=())

    assert out["findings"] == []
    assert out["runs"] == 0
