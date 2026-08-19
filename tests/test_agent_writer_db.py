# -*- coding: utf-8 -*-
import os
import uuid
from datetime import datetime, timedelta

import pytest

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
