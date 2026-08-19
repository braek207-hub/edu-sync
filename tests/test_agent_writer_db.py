# -*- coding: utf-8 -*-
import os
import uuid
from datetime import datetime, timedelta

import pytest

import sync.agent.writer.db as writer_db
from sync.agent.writer.db import WRITER_DDL, ensure_writer_tables, spent_risk
from sync.db import get_connection

REQUIRED = ["edu_agent_actions", "edu_agent_risk_budget"]

# Только test_spent_risk_counts_by_applied_week_not_created_week трогает БД —
# остальные тесты в файле чистые (строковые проверки WRITER_DDL) и без
# DATABASE_URL продолжают выполняться как обычно (конвенция tests/test_ml_db.py
# применяется тут точечно, decorator'ом на одном тесте, а не на весь модуль,
# чтобы не терять покрытие чистых тестов локально).


def test_required_tables_present():
    ddl = "\n".join(WRITER_DDL)
    for table in REQUIRED:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in ddl, table


def test_ddl_has_no_destructive_statements():
    ddl = "\n".join(WRITER_DDL).upper()
    for forbidden in ("DROP TABLE", "TRUNCATE"):
        assert forbidden not in ddl


def test_actions_store_previous_state_for_rollback():
    # Нет сохранённого прошлого состояния — нет применения: откат обязан быть
    # возможен для каждого действия.
    ddl = "\n".join(WRITER_DDL)
    assert "previous_state" in ddl
    assert "red_line" in ddl


def test_actions_have_idempotency_key_unique():
    ddl = "\n".join(WRITER_DDL)
    assert "idempotency_key" in ddl
    assert "UNIQUE" in ddl.upper()


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL")
def test_spent_risk_counts_by_applied_week_not_created_week():
    # Риск-бюджет — деньги под ПРИМЕНЁННЫМИ изменениями. Действие, созданное
    # на прошлой неделе и применённое на текущей, обязано считаться в текущей
    # неделе; действие, созданное и применённое на прошлой, — не считаться.
    # Текстовая проверка исходника не ловит такую регрессию, поэтому здесь —
    # поведенческий тест с реальной БД (конвенция tests/test_ml_db.py).
    ensure_writer_tables()

    now = datetime.utcnow()
    last_week = now - timedelta(days=10)
    week_start = now - timedelta(days=1)
    suffix = uuid.uuid4().hex[:8]
    key_counted = f"test-spent-risk-counted-{suffix}"
    key_excluded = f"test-spent-risk-excluded-{suffix}"

    baseline = spent_risk(week_start.isoformat())
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO edu_agent_actions (
                        action_id, idempotency_key, created_at, applied_at,
                        account, object_level, object_id, action_kind,
                        risk_rub, status
                    ) VALUES (%s, %s, %s, %s, 'test', 'campaign', 'x1',
                              'set_field', 111.0, 'applied')
                    """,
                    (f"test-spent-risk-a-{suffix}", key_counted, last_week, now),
                )
                cur.execute(
                    """
                    INSERT INTO edu_agent_actions (
                        action_id, idempotency_key, created_at, applied_at,
                        account, object_level, object_id, action_kind,
                        risk_rub, status
                    ) VALUES (%s, %s, %s, %s, 'test', 'campaign', 'x2',
                              'set_field', 222.0, 'applied')
                    """,
                    (f"test-spent-risk-b-{suffix}", key_excluded, last_week, last_week),
                )
            conn.commit()

        spent = spent_risk(week_start.isoformat())
        # Только действие, применённое ПОСЛЕ week_start, попадает в сумму —
        # его created_at при этом лежит ДО week_start (проверяет именно
        # applied_at, а не created_at: на старом коде счёт был бы 0).
        assert spent - baseline == pytest.approx(111.0)
    finally:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM edu_agent_actions WHERE idempotency_key IN (%s, %s)",
                    (key_counted, key_excluded),
                )
            conn.commit()


# --------------------- повтор несёт свежие данные, а не прошлое первого прогона
# Дефект 5: ON CONFLICT DO NOTHING оставлял в журнале previous_state ПЕРВОГО
# прогона навсегда. Сценарий: первый прогон сохранил прошлое состояние и упал на
# отправке; человек поправил значение руками; второй прогон прочитал свежий факт,
# но ключ идемпотентности тот же — и откат вернул бы кабинет не туда, откуда
# агент его вывел. То же с оценкой риска и красной линией.


def test_insert_action_updates_unapplied_row_instead_of_ignoring_it():
    sql = " ".join(writer_db.INSERT_ACTION_SQL.split())
    assert "ON CONFLICT (idempotency_key) DO UPDATE SET" in sql
    assert "DO NOTHING" not in sql
    for column in ("previous_state = EXCLUDED.previous_state",
                   "red_line = EXCLUDED.red_line",
                   "risk_rub = EXCLUDED.risk_rub",
                   "payload = EXCLUDED.payload"):
        assert column in sql, column


def test_insert_action_never_touches_applied_row():
    # Уже применённая (или откатанная) строка неприкосновенна: её
    # previous_state описывает реально совершённое изменение и является
    # единственным основанием для отката.
    sql = " ".join(writer_db.INSERT_ACTION_SQL.split())
    assert "WHERE edu_agent_actions.status NOT IN ('applied', 'rolled_back')" in sql


def test_insert_action_resets_stale_response_and_status():
    sql = " ".join(writer_db.INSERT_ACTION_SQL.split())
    assert "status = 'planned'" in sql
    assert "response = '{}'::jsonb" in sql
    assert "created_at = now()" in sql


# ------------------------------------- обрыв ПОСЛЕ отправки виден в отчёте
# Дефект 7: порядок «журнал → отправка» соблюдён, но смерть процесса ПОСЛЕ
# ухода запроса оставляла строку в статусе planned — без ответа и без Id
# созданного объекта. Такую строку не видит ни сторож применённых действий,
# ни откат; риск не списан; diff следующего прогона новых действий не
# предложит, потому что факт в кабинете уже совпал с планом.


def test_stale_planned_finds_rows_stuck_in_intermediate_status():
    sql = " ".join(writer_db.STALE_PLANNED_SQL.split())
    assert "status = 'planned'" in sql
    assert "created_at < now() - make_interval(mins => %s)" in sql


def test_stale_planned_passes_threshold_and_account(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        writer_db, "_fetch",
        lambda sql, params=(): captured.update(sql=sql, params=params) or [],
    )

    writer_db.stale_planned(60, account="acc-1")

    assert captured["params"] == (60, "acc-1", "acc-1")
    # Без кабинета — все зависшие строки, фильтр отключается значением NULL.
    writer_db.stale_planned(30)
    assert captured["params"] == (30, None, None)


def test_open_actions_does_not_see_stuck_planned_rows():
    # Причина, по которой зависшую строку нужно искать отдельным запросом:
    # сторож применённых действий смотрит только на статус applied.
    import inspect

    source = inspect.getsource(writer_db.open_actions)
    assert "status = 'applied'" in source
    assert "planned" not in source
