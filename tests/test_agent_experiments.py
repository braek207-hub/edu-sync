# -*- coding: utf-8 -*-
"""
tests/test_agent_experiments.py — реестр гипотез: ставка живёт как сущность.

До реестра таблица edu_agent_experiments была ПОСМЕРТНЫМ журналом: сторож
писал туда исход уже случившегося действия, а замысел нигде не жил — ни
статуса, ни горизонта, ни критерия успеха. «Сразу увидеть результат и сразу
двигаться дальше» было невозможно технически: возвращаться не к чему.

Главный инвариант, ради которого написан этот файл: ставка и её посмертная
запись — ОДНА строка. Разойдись формулы идентификатора — у каждой гипотезы
оказалось бы две строки: открытая навсегда и закрытая ниоткуда, и обе
выглядели бы корректно.
"""

import hashlib
from datetime import date

import pytest

from sync.agent import experiments
from sync.agent.writer import budget as writer_budget
from sync.agent.writer import db as writer_db
from sync.agent.writer.risk import fit_into_budget


M = 1_000_000
TODAY = date(2026, 8, 26)


# --------------------------------------------------- жизненный цикл

def test_legal_transitions_cover_every_status():
    """Каждый статус описан явно — иначе неизвестный роняет check_transition."""
    assert set(experiments.LEGAL_TRANSITIONS) == (
        set(experiments.OPEN_STATUSES) | set(experiments.CLOSED_STATUSES))
    for closed in experiments.CLOSED_STATUSES:
        assert experiments.LEGAL_TRANSITIONS[closed] == frozenset()


def test_illegal_transition_raises_instead_of_silently_passing():
    """Тихий отказ здесь опаснее падения.

    Ставка осталась бы в прежнем статусе, прогон отчитался бы об успехе, и
    расхождение всплыло бы через две недели на разборе — когда восстановить
    причину уже нечем.
    """
    experiments.check_transition(experiments.STATUS_QUEUED,
                                 experiments.STATUS_RUNNING)
    with pytest.raises(experiments.IllegalTransition):
        experiments.check_transition(experiments.STATUS_WON,
                                     experiments.STATUS_RUNNING)
    with pytest.raises(experiments.IllegalTransition):
        experiments.check_transition("выдуманный", experiments.STATUS_LOST)


def test_queued_can_close_without_ever_running():
    """Действие может не доехать до кабинета вовсе.

    Такая ставка обязана закрываться, а не висеть в очереди вечно; и один
    проход сторожа может увидеть сразу и применение, и пробитую линию —
    промежуточный running был бы в этом случае выдумкой.
    """
    allowed = experiments.LEGAL_TRANSITIONS[experiments.STATUS_QUEUED]
    assert experiments.STATUS_LOST in allowed
    assert experiments.STATUS_ROLLED_BACK in allowed
    assert experiments.STATUS_WON not in allowed


def test_only_improved_counts_as_a_win():
    """«Неопределённо» — не победа.

    Записывать его выигрышем значило бы наполнять контур решений шумом —
    ровно тем, ради чего конвейер и разделён на два контура.
    """
    assert experiments.WINNING_VERDICTS == frozenset({"improved"})
    for verdict in ("inconclusive", "unknown", "harmed"):
        assert verdict not in experiments.WINNING_VERDICTS


# --------------------------------------------------- один объект, не два

def test_registry_and_watchdog_agree_on_the_experiment_id():
    """Ставка и её посмертная запись — одна строка.

    Реестр заводит строку ДО отправки, сторож дописывает исход ПОСЛЕ замера.
    Формула идентификатора обязана быть одна: разойдись они — у каждой
    гипотезы стало бы две строки, открытая навсегда и закрытая ниоткуда.
    Здесь формула сторожа повторена ЯВНО, а не импортирована: тест обязан
    сломаться, если её поменяют на той стороне.
    """
    action_id = "a1b2c3d4e5"
    watchdog_side = hashlib.sha256(
        f"action:{action_id}".encode("utf-8")).hexdigest()[:24]
    assert experiments.experiment_id_for(action_id) == watchdog_side


# --------------------------------------------------- что считается ставкой

def _explored_action():
    """Действие, каким его строит diff_budget для разведочного сдвига."""
    desired = {"111": {"target_28d": 400_000.0, "cost_28d": 100_000.0,
                       "ratio": 4.0, "roi_vs_lambda": 1.3, "p_sign": None,
                       "exploration": True, "exploration_rub": 50_000.0,
                       "confidence_waived": True}}
    state = {"111": {
        "campaign_id": "111", "campaign_type": "TEXT_CAMPAIGN",
        "strategy": {
            "Search": {"BiddingStrategyType": "AVERAGE_CPA",
                       "AverageCpa": {"AverageCpa": 3000 * M, "GoalId": 42,
                                      "WeeklySpendLimit": 20_000 * M}},
            "Network": {"BiddingStrategyType": "SERVING_OFF"}}}}
    actions, _ = writer_budget.diff_budget(desired, state, {"111": 20_000.0})
    assert len(actions) == 1
    return actions[0]


def test_the_exploration_mark_survives_all_the_way_into_the_action():
    """Признак разведки доезжает до действия, а не теряется в планировщике.

    Он объявлен в plan_budget_moves, но payload собирает diff_budget из своих
    полей. Пока признак туда не попадал, единственное применение агента со
    снятым гейтом уверенности было неотличимо от обычного везде, где
    начинается его собственная жизнь: в журнале, в отчёте и в реестре.
    """
    action = _explored_action()
    assert experiments.is_bet(action) is True
    assert action["payload"]["exploration_rub"] == pytest.approx(50_000.0)


def test_an_ordinary_move_is_not_a_bet():
    desired = {"111": {"target_28d": 130_000.0, "cost_28d": 100_000.0,
                       "ratio": 1.3, "roi_vs_lambda": 1.3, "p_sign": 0.99}}
    state = {"111": {
        "campaign_id": "111", "campaign_type": "TEXT_CAMPAIGN",
        "strategy": {
            "Search": {"BiddingStrategyType": "AVERAGE_CPA",
                       "AverageCpa": {"AverageCpa": 3000 * M, "GoalId": 42,
                                      "WeeklySpendLimit": 20_000 * M}},
            "Network": {"BiddingStrategyType": "SERVING_OFF"}}}}
    actions, _ = writer_budget.diff_budget(desired, state, {"111": 20_000.0})
    assert actions and experiments.is_bet(actions[0]) is False
    assert "exploration" not in actions[0]["payload"]


# --------------------------------------------------- откуда деньги

def test_stake_is_the_price_the_risk_budget_actually_charged():
    """Ставку оплачивает существующий карман, третьего здесь нет.

    Число берётся из действия, а не считается заново: его уже списал
    fit_into_budget, и второй расчёт был бы вторым источником правды о цене.
    Проверяется сквозь настоящий риск-бюджет — имя поля живёт на его стороне.
    """
    action = _explored_action()
    fits, deferred = fit_into_budget(
        [action], {action["idempotency_key"]: 1234.5}, remaining_rub=10_000.0)
    assert not deferred
    assert experiments.stake_of(fits[0]) == pytest.approx(1234.5)


def test_an_action_the_risk_budget_never_priced_is_not_a_bet():
    """Гипотеза, которая сама назначает себе бюджет, — тот самый третий карман."""
    with pytest.raises(experiments.StakeNotCharged):
        experiments.stake_of(_explored_action())


# --------------------------------------------------- заведение ставки

def _priced_bet():
    action = _explored_action()
    action = {**action,
              "risk_rub": 1234.5,
              "red_line": {"max_value": 2500.0, "metric": "cpa"},
              "risk_basis": "delta"}
    return action


def test_open_bet_records_all_four_things_before_the_action_leaves():
    """Ставка, красная линия, горизонт и критерий — все до отправки.

    Задним числом ни одно из четырёх не восстановить: перепланировка
    переписывает red_line ещё не применённого действия, а критерий, названный
    после замера, всегда совпадает с результатом.
    """
    action = _priced_bet()
    row = experiments.open_bet(action, "act-1", TODAY)
    assert row["status"] == experiments.STATUS_QUEUED
    assert row["stake_rub"] == pytest.approx(1234.5)
    assert row["stake_source"] == experiments.STAKE_SOURCE
    assert row["horizon_days"] == experiments.HORIZON_DAYS
    assert row["success_criterion"]
    assert row["red_line"] == {"max_value": 2500.0, "metric": "cpa"}
    assert row["started_on"] == "2026-08-26"
    assert row["idempotency_key"] == action["idempotency_key"]
    assert row["params"]["exploration_rub"] == pytest.approx(50_000.0)


def test_red_line_is_stored_as_a_copy_not_a_reference():
    """Строка действия перепланировкой переписывается, ставка — нет."""
    action = _priced_bet()
    row = experiments.open_bet(action, "act-1", TODAY)
    action["red_line"]["max_value"] = 99999.0
    assert row["red_line"]["max_value"] == pytest.approx(2500.0)


def test_success_criterion_names_the_red_line_ceiling():
    """Критерий читается человеком на разборе — без похода в код сторожа."""
    action = _priced_bet()
    row = experiments.open_bet(action, "act-1", TODAY)
    assert "2500.0" in row["success_criterion"]


def test_the_bet_id_matches_what_the_watchdog_will_write_later():
    """Сквозная проверка шва Э1 → сторож на настоящем action_id.

    Э1 выводит action_id из ключа идемпотентности (make_action_id) ещё до
    записи в журнал; журнал позже кладёт в строку ровно его. Сойтись обязаны
    все три.
    """
    action = _priced_bet()
    action_id = writer_db.make_action_id(action["idempotency_key"])
    row = experiments.open_bet(action, action_id, TODAY)
    assert row["experiment_id"] == experiments.experiment_id_for(action_id)
    assert writer_db.make_action_id(action["idempotency_key"]) == action_id


# --------------------------------------------------- закрытие ставки

def _open_row(**over):
    row = {"experiment_id": "e1", "status": experiments.STATUS_RUNNING,
           "started_on": "2026-08-01", "horizon_days": 14,
           "applied_at": "2026-08-01"}
    row.update(over)
    return row


def test_a_broken_red_line_closes_the_bet_ahead_of_any_verdict():
    """Порядок проверок — от необратимого к ожидаемому.

    Откат уже случился, мерить дальше нечего, и вердикт по горизонту его не
    перебивает.
    """
    step = experiments.settle(
        _open_row(rolled_back_at="2026-08-05", observation_verdict="improved"),
        TODAY)
    assert step["status"] == experiments.STATUS_ROLLED_BACK


def test_verdict_of_the_watchdog_becomes_the_status():
    won = experiments.settle(_open_row(observation_verdict="improved"), TODAY)
    lost = experiments.settle(_open_row(observation_verdict="harmed"), TODAY)
    assert won["status"] == experiments.STATUS_WON
    assert lost["status"] == experiments.STATUS_LOST


def test_observation_closed_without_a_verdict_is_a_loss():
    """Деньги потрачены, ответ не куплен.

    Прятать это в «ещё наблюдаем» значило бы держать открытым то, что уже
    никогда не закроется.
    """
    step = experiments.settle(
        _open_row(observation_closed_at="2026-08-20"), TODAY)
    assert step["status"] == experiments.STATUS_LOST


def test_applied_action_moves_the_bet_out_of_the_queue():
    step = experiments.settle(
        _open_row(status=experiments.STATUS_QUEUED, applied_at="2026-08-20"),
        TODAY)
    assert step["status"] == experiments.STATUS_RUNNING


def test_a_bet_whose_action_never_arrived_closes_on_the_horizon():
    """Иначе очередь реестра копит замыслы, которых уже нет."""
    step = experiments.settle(
        _open_row(status=experiments.STATUS_QUEUED, applied_at=None,
                  started_on="2026-08-01"), TODAY)
    assert step["status"] == experiments.STATUS_LOST


def test_a_young_bet_is_left_alone():
    assert experiments.settle(
        _open_row(status=experiments.STATUS_QUEUED, applied_at=None,
                  started_on="2026-08-25"), TODAY) is None
    assert experiments.settle(_open_row(), TODAY) is None


def test_the_horizon_counts_from_the_day_the_change_landed():
    """Наблюдение начинается в кабинете, а не в замысле.

    Отложенное риск-бюджетом действие уезжает на следующий прогон, и отсчёт
    от заведения украл бы у него часть горизонта.
    """
    late = _open_row(started_on="2026-08-01", applied_at="2026-08-20")
    assert experiments.horizon_passed(late, TODAY) is False
    assert experiments.horizon_passed(
        {**late, "applied_at": None}, TODAY) is True


# --------------------------------------------------- проход сторожа

def _settle_env(monkeypatch, rows, moved_ok=True):
    """Реестр в памяти вместо базы. Возвращает список записанных переводов."""
    from sync import agent_e1_watchdog as watchdog

    written = []

    def _move(experiment_id, from_status, status, reason, closed_statuses):
        written.append({"experiment_id": experiment_id,
                        "from_status": from_status, "status": status,
                        "reason": reason, "closed": list(closed_statuses)})
        return moved_ok

    monkeypatch.setattr(watchdog.agent_db, "load_open_hypotheses",
                        lambda statuses: list(rows))
    monkeypatch.setattr(watchdog.agent_db, "move_hypothesis", _move)
    return watchdog, written


def test_watchdog_closes_the_bets_whose_time_has_come(monkeypatch):
    """Один проход сторожа: что закрылось, то и записано."""
    rows = [
        _open_row(experiment_id="won", observation_verdict="improved"),
        _open_row(experiment_id="lost", observation_verdict="harmed"),
        _open_row(experiment_id="young"),
    ]
    watchdog, written = _settle_env(monkeypatch, rows)
    out = watchdog.settle_hypotheses(TODAY)
    assert out["open_before"] == 3
    assert out["moved"] == 2
    assert out["still_open"] == 1
    assert out["by_status"] == {experiments.STATUS_WON: 1,
                                experiments.STATUS_LOST: 1}
    assert {w["experiment_id"] for w in written} == {"won", "lost"}
    # Закрытые статусы едут в запрос списком: что считать закрытым, решает
    # жизненный цикл, а не SQL.
    assert written[0]["closed"] == list(experiments.CLOSED_STATUSES)


def test_a_rehearsal_settles_on_paper_only(monkeypatch):
    """Репетиция считает и печатает, в базу не пишет."""
    watchdog, written = _settle_env(
        monkeypatch, [_open_row(observation_verdict="improved")])
    out = watchdog.settle_hypotheses(TODAY, journal_ok=False)
    assert out["moved"] == 1
    assert written == []


def test_a_bet_taken_by_another_run_is_not_an_error(monkeypatch):
    """Гонка двух прогонов: исход записан, просто не нами.

    Гард "status = from_status" в UPDATE не даёт закрыть ставку дважды и
    записать два исхода. Проигранная гонка обязана быть видна числом, а не
    молчанием, — иначе «сторож ничего не нашёл» и «сторож опоздал» в отчёте
    неотличимы.
    """
    watchdog, _ = _settle_env(
        monkeypatch, [_open_row(observation_verdict="improved")],
        moved_ok=False)
    out = watchdog.settle_hypotheses(TODAY)
    assert out["moved"] == 0
    assert out["lost_race"] == 1


def test_an_impossible_status_is_reported_not_swallowed(monkeypatch):
    """В реестре состояние, которого жизненный цикл не предусматривает.

    Молчание здесь пряталось бы до разбора через две недели, когда причину
    уже не восстановить.
    """
    watchdog, written = _settle_env(
        monkeypatch,
        [_open_row(status="выдуманный", observation_verdict="improved")])
    out = watchdog.settle_hypotheses(TODAY)
    assert out["moved"] == 0
    assert written == []
    assert out["illegal_transitions"][0]["status"] == "выдуманный"
