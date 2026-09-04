# -*- coding: utf-8 -*-
"""
sync/agent/approval_db.py — память апрув-контура: решения человека и курсор
Telegram.

Две таблицы, обе создаются как остальные таблицы агента — CREATE TABLE IF
NOT EXISTS при первом обращении:

  • edu_agent_approvals — журнал решений человека. Читают его двое: воркер
    (идемпотентность — одно решение не применяется дважды) и Э1
    (vetoed_keys: план пересчитывается каждый такт с тем же ключом, и без
    этой памяти вчерашнее «нет» стиралось бы сегодняшней вставкой строки).
  • edu_agent_approver_state — одна строка с last_update_id getUpdates.
    Без курсора каждый прогон перечитывал бы весь неподтверждённый хвост
    чата и вечно держал его в Telegram.
"""

from typing import Any, Dict, Iterable, List, Optional

from sync.db import get_connection

SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS edu_agent_approvals (
      idempotency_key  TEXT PRIMARY KEY,
      action_id        TEXT NOT NULL,
      decision         TEXT NOT NULL,          -- approved | vetoed | expired
      decided_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
      update_id        BIGINT
    );
    CREATE TABLE IF NOT EXISTS edu_agent_approver_state (
      id               INT PRIMARY KEY DEFAULT 1,
      last_update_id   BIGINT NOT NULL DEFAULT 0,
      updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
    );
"""

DECISION_APPROVED = "approved"
DECISION_VETOED = "vetoed"
DECISION_EXPIRED = "expired"

# Сколько дней вето человека держит действие вне очереди. Не навсегда:
# экономика кампании меняется, и через две недели тот же ключ — уже другое
# по смыслу решение на других данных.
VETO_MEMORY_DAYS = 14


def ensure_schema() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        conn.commit()


def record_decision(idempotency_key: str, action_id: str, decision: str,
                    update_id: Optional[int] = None) -> None:
    """Решение человека (или TTL) — в журнал. Повтор перезаписывает:
    позднее слово человека сильнее раннего."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO edu_agent_approvals
                       (idempotency_key, action_id, decision, update_id)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (idempotency_key) DO UPDATE
                   SET decision = EXCLUDED.decision,
                       decided_at = now(),
                       update_id = EXCLUDED.update_id
                """,
                (str(idempotency_key), str(action_id), str(decision), update_id))
        conn.commit()


def vetoed_keys(days: int = VETO_MEMORY_DAYS) -> List[str]:
    """Ключи, по которым человек недавно сказал «нет». Только vetoed:
    expired — молчание, оно не запрещает спросить завтра."""
    ensure_schema()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT idempotency_key FROM edu_agent_approvals
                 WHERE decision = %s
                   AND decided_at >= now() - make_interval(days => %s)
                """,
                (DECISION_VETOED, int(days)))
            return [str(r[0]) for r in cur.fetchall()]


def decisions_for(keys: Iterable[str]) -> Dict[str, str]:
    """Решения человека по этим ключам идемпотентности: ключ → approved|vetoed.

    Источник решений, когда ответы принимает вебхук Panda-BI, а не getUpdates:
    бот пишет слово человека сюда тем же INSERT ... ON CONFLICT, а воркер его
    отсюда читает. Одновременно webhook и getUpdates у Bot API невозможны, и
    после переезда на вебхук воркер обязан смотреть в базу — иначе он ослепнет.

    Строка со статусом pending_approval в журнале и approved здесь означает
    «человек сказал да, применение ещё не состоялось»: применённое действие
    из pending уходит статусом, а не удалением решения.
    """
    keys = [str(k) for k in keys if str(k)]
    if not keys:
        return {}
    ensure_schema()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT idempotency_key, decision FROM edu_agent_approvals
                 WHERE idempotency_key = ANY(%s)
                   AND decision IN (%s, %s)
                """,
                (keys, DECISION_APPROVED, DECISION_VETOED))
            return {str(r[0]): str(r[1]) for r in cur.fetchall()}


def get_offset() -> int:
    ensure_schema()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT last_update_id FROM edu_agent_approver_state WHERE id = 1")
            row = cur.fetchone()
            return int(row[0]) if row else 0


def set_offset(update_id: int) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO edu_agent_approver_state (id, last_update_id)
                VALUES (1, %s)
                ON CONFLICT (id) DO UPDATE
                   SET last_update_id = GREATEST(
                           edu_agent_approver_state.last_update_id,
                           EXCLUDED.last_update_id),
                       updated_at = now()
                """,
                (int(update_id),))
        conn.commit()


def load_pending() -> List[Dict[str, Any]]:
    """Очередь на апрув — строки журнала действий в pending_approval."""
    from sync.agent import approval

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT action_id, idempotency_key, account, object_id,
                       action_kind, payload, risk_rub, created_at
                  FROM edu_agent_actions
                 WHERE status = %s
                 ORDER BY created_at
                """,
                (approval.PENDING_STATUS,))
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


def expire_pending(ttl_hours: int) -> List[str]:
    """Просроченные pending → rejected(approval_expired). Возвращает action_id.

    Гард по статусу — как везде в writer/db: строку, уже забранную другим
    контуром (воркер применил её секунду назад), трогать нельзя.
    """
    from sync.agent import approval

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE edu_agent_actions
                   SET status = 'rejected',
                       response = jsonb_build_object(
                           'approval', 'expired',
                           'ttl_hours', %s::int)
                 WHERE status = %s
                   AND created_at < now() - make_interval(hours => %s)
                RETURNING action_id, idempotency_key
                """,
                (int(ttl_hours), approval.PENDING_STATUS, int(ttl_hours)))
            rows = cur.fetchall()
        conn.commit()
    for action_id, key in rows:
        record_decision(str(key), str(action_id), DECISION_EXPIRED)
    return [str(r[0]) for r in rows]


def claim_for_apply(action_id: str) -> bool:
    """pending_approval → planned: строка возвращается в обычную машину
    статусов (mark_sent требует 'planned'). True — забрали мы."""
    from sync.agent import approval

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE edu_agent_actions SET status = 'planned'
                 WHERE action_id = %s AND status = %s
                """,
                (str(action_id), approval.PENDING_STATUS))
            claimed = cur.rowcount == 1
        conn.commit()
    return claimed


def release_claim(action_id: str) -> bool:
    """planned → pending_approval обратно: репетиция воркера строку не ест."""
    from sync.agent import approval

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE edu_agent_actions SET status = %s
                 WHERE action_id = %s AND status = 'planned'
                """,
                (approval.PENDING_STATUS, str(action_id)))
            released = cur.rowcount == 1
        conn.commit()
    return released


def mark_vetoed(action_id: str) -> bool:
    """pending_approval → rejected по слову человека."""
    from sync.agent import approval

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE edu_agent_actions
                   SET status = 'rejected',
                       response = jsonb_build_object('approval', 'human_veto')
                 WHERE action_id = %s AND status = %s
                """,
                (str(action_id), approval.PENDING_STATUS))
            marked = cur.rowcount == 1
        conn.commit()
    return marked
