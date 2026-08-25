# -*- coding: utf-8 -*-
"""Карта перезапуска обучения: какие действия сбивают стратегию и как часто их можно.

Источник классификации — справка Директа (обучение начинается заново при смене
стратегии, модели атрибуции и оплаты, изменении ограничения расхода,
корректировке целевых действий, остановке кампании дольше семи дней).
"""

from datetime import date

from sync.agent.writer.learning import (
    BUDGET_SAFE_DELTA, LEARNING_COOLDOWN_DAYS, learning_impact,
    split_by_learning_cooldown,
)


def _budget(new_micros, old_micros):
    return {"action_kind": "budget.set",
            "payload": {"WeeklySpendLimit": new_micros},
            "previous_state": {"WeeklySpendLimit": old_micros}}


def test_tcpa_and_suspend_reset_learning():
    assert learning_impact({"action_kind": "tcpa.set"}) == "resets"
    assert learning_impact({"action_kind": "campaign.suspend"}) == "resets"


def test_small_budget_change_is_safe():
    # ±20 % недельного бюджета обучение не сбивают (практика ведения кабинетов).
    assert learning_impact(_budget(1_200_000_000, 1_000_000_000)) == "safe"
    assert learning_impact(_budget(850_000_000, 1_000_000_000)) == "safe"


def test_big_budget_change_resets():
    assert learning_impact(_budget(1_500_000_000, 1_000_000_000)) == "resets"
    assert learning_impact(_budget(500_000_000, 1_000_000_000)) == "resets"


def test_daily_budget_uses_same_threshold():
    action = {"action_kind": "budget.set_daily",
              "payload": {"DailyBudget": {"Amount": 110_000_000}},
              "previous_state": {"DailyBudget": {"Amount": 100_000_000}}}
    assert learning_impact(action) == "safe"


def test_budget_without_previous_value_is_unknown():
    # Прежнего лимита не прочитали — величину изменения не посчитать, и
    # «безопасно» здесь было бы догадкой.
    assert learning_impact({"action_kind": "budget.set",
                            "payload": {"WeeklySpendLimit": 1_000_000_000},
                            "previous_state": {}}) == "unknown"


def test_modifiers_and_lists_are_safe():
    assert learning_impact({"action_kind": "bidmodifier.set"}) == "safe"
    assert learning_impact({"action_kind": "bidmodifier.add"}) == "safe"
    assert learning_impact({"action_kind": "negative.add"}) == "safe"
    assert learning_impact({"action_kind": "placement.exclude"}) == "safe"


def test_schedule_is_unknown_not_safe():
    # Временного таргетинга в списке справки нет, но он меняет объём показов.
    # Записать его в безопасные значило бы выдать незнание за знание.
    assert learning_impact({"action_kind": "schedule.set"}) == "unknown"


def test_new_action_kind_is_unknown_by_default():
    assert learning_impact({"action_kind": "campaign.resume"}) == "unknown"


def test_safe_actions_pass_cooldown_untouched():
    actions = [{"object_id": "111", "action_kind": "bidmodifier.set"}]
    allowed, blocked = split_by_learning_cooldown(
        actions, {"111": date(2026, 8, 20)}, today=date(2026, 8, 25))
    assert allowed == actions
    assert blocked == []


def test_resetting_action_blocked_inside_cooldown():
    actions = [{"object_id": "111", **_budget(1_500_000_000, 1_000_000_000)}]
    allowed, blocked = split_by_learning_cooldown(
        actions, {"111": date(2026, 8, 20)}, today=date(2026, 8, 25))
    assert allowed == []
    assert len(blocked) == 1
    assert str(LEARNING_COOLDOWN_DAYS) in blocked[0]["blocked_reason"]
    assert blocked[0]["last_learning_reset_at"] == "2026-08-20"


def test_resetting_action_passes_after_cooldown():
    actions = [{"object_id": "111", "action_kind": "tcpa.set"}]
    allowed, blocked = split_by_learning_cooldown(
        actions, {"111": date(2026, 8, 1)}, today=date(2026, 8, 25))
    assert allowed == actions
    assert blocked == []


def test_object_without_history_passes():
    actions = [{"object_id": "222", **_budget(1_500_000_000, 1_000_000_000)}]
    allowed, blocked = split_by_learning_cooldown(actions, {}, today=date(2026, 8, 25))
    assert len(allowed) == 1 and blocked == []


def test_small_budget_step_passes_inside_cooldown():
    # Главный смысл порога: перелив в пределах ±20 % идёт каждый такт, даже
    # если стратегию перезапускали вчера. Иначе перераспределение встало бы.
    actions = [{"object_id": "111", **_budget(1_150_000_000, 1_000_000_000)}]
    allowed, blocked = split_by_learning_cooldown(
        actions, {"111": date(2026, 8, 24)}, today=date(2026, 8, 25))
    assert len(allowed) == 1 and blocked == []


def test_unknown_class_is_treated_as_resetting():
    # Осторожная сторона: неизвестное действие внутри кулдауна не проходит.
    actions = [{"object_id": "111", "action_kind": "schedule.set"}]
    allowed, blocked = split_by_learning_cooldown(
        actions, {"111": date(2026, 8, 24)}, today=date(2026, 8, 25))
    assert allowed == []
    assert blocked[0]["learning_impact"] == "unknown"


def test_two_resetting_actions_on_one_object_in_one_run():
    # Кулдаун смотрит и на уже отобранное в этом же прогоне: два сбрасывающих
    # изменения подряд — это два перезапуска обучения одной кампании за день.
    actions = [{"object_id": "111", "action_kind": "budget.set"},
               {"object_id": "111", "action_kind": "tcpa.set"}]
    allowed, blocked = split_by_learning_cooldown(actions, {}, today=date(2026, 8, 25))
    assert len(allowed) == 1
    assert len(blocked) == 1


def test_iso_string_history_is_read_like_a_date():
    # Журнал отдаёт дату типом date, но отчёт и тесты носят её строкой —
    # разбор обязан принимать обе формы, иначе кулдаун молча не сработает.
    actions = [{"object_id": "111", "action_kind": "tcpa.set"}]
    allowed, blocked = split_by_learning_cooldown(
        actions, {"111": "2026-08-24"}, today=date(2026, 8, 25))
    assert allowed == [] and len(blocked) == 1


def test_cooldown_days_is_the_same_number_as_the_money_knob_gate():
    # Кулдаун обучения и кулдаун денежных ручек (writer/budget.apply_cooldown,
    # применяется в agent_e1) — одно правило: «не чаще, чем стратегия успевает
    # выучиться». Два числа разъехались бы при первой же правке одного из них.
    from sync.agent.writer import budget

    assert LEARNING_COOLDOWN_DAYS == budget.BUDGET_COOLDOWN_DAYS


def test_budget_write_cap_keeps_engine_inside_the_safe_class():
    # Инвариант, а не отбор: кап шага записи (±20 % от расхода) не даёт
    # движку породить сбрасывающее бюджетное действие вовсе. Если кап когда-то
    # ослабят, этот тест обязан упасть первым — вместе с ним меняется и смысл
    # классификации «по величине».
    from sync.agent.writer import budget

    assert budget.MAX_WRITE_STEP <= BUDGET_SAFE_DELTA


# --------------- чтение истории сбросов из журнала (writer/db.py)


def _captured_sql(monkeypatch, call):
    """Текст запроса, который функция реально отправляет в БД."""
    from sync.agent.writer import db as writer_db

    captured = {}
    monkeypatch.setattr(
        writer_db, "_fetch",
        lambda sql, params=(): captured.update(sql=sql, params=params) or [],
    )
    call()
    return " ".join(captured["sql"].split()), captured.get("params")


def test_last_learning_reset_counts_only_applied_resetting_actions(monkeypatch):
    # История сбросов: только применённые строки, только классы resets и
    # unknown, только этот кабинет. Планировавшееся и не ушедшее в кабинет
    # обучение не сбивало, а старые строки с NULL судить нечем.
    from sync.agent.writer import db as writer_db

    sql, params = _captured_sql(
        monkeypatch, lambda: writer_db.last_learning_reset("acc"))

    assert "applied_at IS NOT NULL" in sql
    assert "learning_impact IN ('resets', 'unknown')" in sql
    expected = ", ".join("'%s'" % x for x in writer_db.RISK_CHARGED_STATUSES)
    assert "status IN (%s)" % expected in sql
    assert "max(applied_at::date)" in sql
    assert params == ("acc", "acc")


def test_last_learning_reset_returns_date_by_object(monkeypatch):
    from sync.agent.writer import db as writer_db

    monkeypatch.setattr(
        writer_db, "_fetch",
        lambda sql, params=(): [{"object_id": 111,
                                 "last_reset": date(2026, 8, 20)}])

    assert writer_db.last_learning_reset("acc") == {"111": date(2026, 8, 20)}


def test_journal_row_carries_the_learning_class(monkeypatch):
    # Колонка журнала и её место в INSERT: без неё класс терялся бы по дороге,
    # и история сбросов оставалась бы пустой при любом числе применений.
    from sync.agent.writer import db as writer_db

    assert "learning_impact" in " ".join(writer_db.WRITER_DDL)
    assert "%(learning_impact)s" in writer_db.INSERT_ACTION_SQL
    assert "learning_impact = EXCLUDED.learning_impact" in writer_db.INSERT_ACTION_SQL
