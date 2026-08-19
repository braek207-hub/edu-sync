# -*- coding: utf-8 -*-
"""
sync/agent/writer/db.py — таблицы движка записи и доступ к ним.

Журнал действий — не логи, а рабочий механизм: из него берётся предыдущее
состояние для отката и ключ идемпотентности, чтобы повторный прогон не
отправил тот же запрос дважды.
"""

import hashlib
import json
from typing import Any, Dict, List, Optional

import psycopg2.extras

from sync.db import get_connection

WRITER_DDL: List[str] = [
    # Журнал действий: что собирались сделать, что было до, что ответил API.
    """
    CREATE TABLE IF NOT EXISTS edu_agent_actions (
      action_id        TEXT PRIMARY KEY,
      idempotency_key  TEXT NOT NULL UNIQUE,
      created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
      applied_at       TIMESTAMPTZ,
      account          TEXT NOT NULL,
      object_level     TEXT NOT NULL,
      object_id        TEXT NOT NULL,
      action_kind      TEXT NOT NULL,
      payload          JSONB NOT NULL DEFAULT '{}'::jsonb,
      previous_state   JSONB NOT NULL DEFAULT '{}'::jsonb,
      red_line         JSONB NOT NULL DEFAULT '{}'::jsonb,
      risk_rub         DOUBLE PRECISION NOT NULL DEFAULT 0,
      status           TEXT NOT NULL DEFAULT 'planned',
      response         JSONB NOT NULL DEFAULT '{}'::jsonb,
      rolled_back_at   TIMESTAMPTZ
    )
    """,
    # Недельный риск-бюджет: сколько денег под непроверенными изменениями.
    """
    CREATE TABLE IF NOT EXISTS edu_agent_risk_budget (
      week_start   DATE PRIMARY KEY,
      limit_rub    DOUBLE PRECISION NOT NULL,
      note         TEXT
    )
    """,
]


def ensure_writer_tables() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            for statement in WRITER_DDL:
                cur.execute(statement)
        conn.commit()


def make_action_id(idempotency_key: str) -> str:
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]


def _fetch(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def find_action_by_key(idempotency_key: str) -> Optional[Dict[str, Any]]:
    rows = _fetch(
        "SELECT * FROM edu_agent_actions WHERE idempotency_key = %s", (idempotency_key,)
    )
    return rows[0] if rows else None


def insert_action(row: Dict[str, Any]) -> str:
    """Пишет действие в статусе planned. Повторный вызов с тем же ключом
    возвращает уже существующий action_id и ничего не меняет."""
    action_id = make_action_id(row["idempotency_key"])
    sql = """
        INSERT INTO edu_agent_actions (
            action_id, idempotency_key, account, object_level, object_id,
            action_kind, payload, previous_state, red_line, risk_rub, status
        ) VALUES (
            %(action_id)s, %(idempotency_key)s, %(account)s, %(object_level)s,
            %(object_id)s, %(action_kind)s, %(payload)s, %(previous_state)s,
            %(red_line)s, %(risk_rub)s, 'planned'
        )
        ON CONFLICT (idempotency_key) DO NOTHING
    """
    params = {
        **row,
        "action_id": action_id,
        "payload": json.dumps(row.get("payload", {}), ensure_ascii=False),
        "previous_state": json.dumps(row.get("previous_state", {}), ensure_ascii=False),
        "red_line": json.dumps(row.get("red_line", {}), ensure_ascii=False),
    }
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    return action_id


def mark_action(action_id: str, status: str, response: Dict[str, Any]) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE edu_agent_actions
                   SET status = %s,
                       response = %s,
                       applied_at = CASE WHEN %s = 'applied' THEN now() ELSE applied_at END
                 WHERE action_id = %s
                """,
                (status, json.dumps(response, ensure_ascii=False), status, action_id),
            )
        conn.commit()


def open_actions() -> List[Dict[str, Any]]:
    """Применённые и ещё не откатанные — за ними следит сторож красных линий."""
    return _fetch(
        "SELECT * FROM edu_agent_actions WHERE status = 'applied' AND rolled_back_at IS NULL"
    )


def mark_rolled_back(action_id: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE edu_agent_actions SET rolled_back_at = now(), status = 'rolled_back' "
                "WHERE action_id = %s",
                (action_id,),
            )
        conn.commit()


def spent_risk(week_start: str) -> float:
    rows = _fetch(
        """
        SELECT COALESCE(SUM(risk_rub), 0) AS spent
        FROM edu_agent_actions
        WHERE status = 'applied'
          AND rolled_back_at IS NULL
          AND applied_at >= %s
        """,
        (week_start,),
    )
    return float(rows[0]["spent"]) if rows else 0.0


def risk_limit(week_start: str, default_rub: float) -> float:
    rows = _fetch(
        "SELECT limit_rub FROM edu_agent_risk_budget WHERE week_start = %s", (week_start,)
    )
    return float(rows[0]["limit_rub"]) if rows else default_rub
