# -*- coding: utf-8 -*-
import inspect

from sync.agent.writer.db import WRITER_DDL, spent_risk

REQUIRED = ["edu_agent_actions", "edu_agent_risk_budget"]


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


def test_spent_risk_filters_by_applied_at_not_created_at():
    # Риск-бюджет — деньги под ПРИМЕНЁННЫМИ изменениями. Действие, созданное
    # в одну неделю и применённое в другую, обязано списываться с недели
    # применения, иначе гейт риска считает по чужой неделе и пропускает
    # больше изменений, чем разрешено.
    source = inspect.getsource(spent_risk)
    assert "applied_at >=" in source
    assert "created_at >=" not in source
