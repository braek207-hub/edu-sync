# -*- coding: utf-8 -*-
"""Хранилище модулей Директа: project_modules, module_runs, module_findings.

Схему ведёт миграция Panda-BI (supabase/migrations/20261018193100_direct_modules.sql);
здесь таблицы не создаются.
"""

import json
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, List, Optional, Sequence

import psycopg2.extras

from sync import db
from sync.modules.lifecycle import (FAILED_RETRY_HOURS, LIVE_STATUSES, REJECT_SUPPRESS_DAYS,
                                    Reconciled, can_transition)
from sync.modules.types import ApplyResult, StoredFinding, fingerprint

_FINDING_COLS = ("id, status, fingerprint, last_seen_at, decided_at, outcome_at, rule, severity, title, "
                 "login, campaign_id, campaign_name, object_type, object_id, details, action")


def _json(value):
    # ensure_ascii=False: кириллица в названиях кампаний должна лечь в jsonb как есть
    return psycopg2.extras.Json(value, dumps=lambda o: json.dumps(o, ensure_ascii=False))


def _stored(row: dict) -> StoredFinding:
    return StoredFinding(
        id=row["id"], status=row["status"], fingerprint=row["fingerprint"],
        last_seen_at=row["last_seen_at"], decided_at=row["decided_at"], outcome_at=row["outcome_at"],
        rule=row["rule"], severity=row["severity"], title=row["title"],
        login=row["login"] or "", campaign_id=row["campaign_id"],
        campaign_name=row["campaign_name"] or "", object_type=row["object_type"] or "",
        object_id=row["object_id"] or "", details=row["details"] or {}, action=row["action"],
    )


def _dict_cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def load_project_modules(slug: str) -> List[dict]:
    """Только включённые модули: off раннер не трогает."""
    with db.get_connection() as conn, _dict_cursor(conn) as cur:
        cur.execute(
            "SELECT module_key, mode, settings, run_requested_at FROM project_modules "
            "WHERE dashboard_slug = %s AND mode <> 'off' ORDER BY module_key", (slug,))
        return [dict(r) for r in cur.fetchall()]


def last_run_started(slug: str, module_key: str,
                     statuses: Optional[Sequence[str]] = None) -> Optional[datetime]:
    """Старт последнего прогона; statuses сужает до завершённых этими статусами."""
    st = list(statuses) if statuses else None
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT max(started_at) FROM module_runs "
                    "WHERE dashboard_slug = %s AND module_key = %s "
                    "AND (%s::text[] IS NULL OR status = ANY(%s))",
                    (slug, module_key, st, st))
        return cur.fetchone()[0]


def start_run(slug: str, module_key: str, mode: str) -> int:
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO module_runs (dashboard_slug, module_key, mode) "
                    "VALUES (%s, %s, %s) RETURNING id", (slug, module_key, mode))
        return cur.fetchone()[0]


def finish_run(run_id: int, status: str, summary: dict, error: Optional[str] = None) -> None:
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE module_runs SET finished_at = now(), status = %s, summary = %s, "
                    "error = %s WHERE id = %s", (status, _json(summary), error, run_id))


def existing_findings(slug: str, module_key: str) -> List[StoredFinding]:
    """Всё, что reconcile нужно, чтобы не вставить повторно и не потерять живое."""
    with db.get_connection() as conn, _dict_cursor(conn) as cur:
        cur.execute(
            f"SELECT {_FINDING_COLS} FROM module_findings "
            "WHERE dashboard_slug = %s AND module_key = %s AND ("
            "  status = ANY(%s) OR status = 'dismissed' "
            "  OR (status = 'rejected' "
            "      AND coalesce(decided_at, last_seen_at) > now() - make_interval(days => %s)) "
            "  OR (status = 'rolled_back' AND coalesce(outcome_at, decided_at, last_seen_at) "
            "      > now() - make_interval(days => %s)) "
            "  OR (status = 'failed' AND coalesce(outcome_at, decided_at, last_seen_at) "
            "      > now() - make_interval(hours => %s)))",
            (slug, module_key, list(LIVE_STATUSES), REJECT_SUPPRESS_DAYS, REJECT_SUPPRESS_DAYS,
             FAILED_RETRY_HOURS))
        return [_stored(r) for r in cur.fetchall()]


def apply_reconciled(slug: str, module_key: str, run_id: int, r: Reconciled) -> List[int]:
    """Одна транзакция на весь итог прогона; вернуть id вставленных находок."""
    inserted: List[int] = []
    with db.get_connection() as conn, conn.cursor() as cur:
        for fid, f in r.touch:
            # status = 'open': одобренную человеком находку прогон переписывать не вправе
            cur.execute(
                "UPDATE module_findings SET last_seen_at = now(), run_id = %s, details = %s, "
                "action = %s, title = %s, severity = %s WHERE id = %s AND status = 'open'",
                (run_id, _json(f.details), _json(f.action) if f.action is not None else None,
                 f.title, f.severity, fid))
        if r.seen_live:
            # только метка «видели»: одобренное/применённое прогон не переписывает
            cur.execute("UPDATE module_findings SET last_seen_at = now(), run_id = %s "
                        "WHERE id = ANY(%s) AND status IN ('approved', 'applied', "
                        "'rollback_requested')", (run_id, list(r.seen_live)))
        for new_status, ids in (("resolved", r.resolve), ("expired", r.expire)):
            if ids:
                cur.execute("UPDATE module_findings SET status = %s WHERE id = ANY(%s) "
                            "AND status = 'open'", (new_status, list(ids)))
        for f in r.insert:
            # DO NOTHING: параллельный прогон мог вставить ту же живую находку первым
            cur.execute(
                "INSERT INTO module_findings (dashboard_slug, module_key, run_id, fingerprint, "
                "rule, severity, login, campaign_id, campaign_name, object_type, object_id, "
                "title, details, action) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING RETURNING id",
                (slug, module_key, run_id, fingerprint(module_key, f), f.rule, f.severity,
                 f.login, f.campaign_id, f.campaign_name, f.object_type, f.object_id, f.title,
                 _json(f.details), _json(f.action) if f.action is not None else None))
            row = cur.fetchone()
            if row:
                inserted.append(row[0])
    return inserted


def pending(slug: str) -> List[StoredFinding]:
    """Что раннер обязан сделать: одобренное применить, запрошенный откат снять."""
    with db.get_connection() as conn, _dict_cursor(conn) as cur:
        cur.execute(
            f"SELECT {_FINDING_COLS} FROM module_findings WHERE dashboard_slug = %s "
            "AND status IN ('approved', 'rollback_requested') ORDER BY id", (slug,))
        return [_stored(r) for r in cur.fetchall()]


def set_outcome(finding_id: int, status: str, result: ApplyResult,
                rollback: bool = False) -> bool:
    """Итог применения (rollback=False: approved -> applied/failed) или отката
    (rollback=True: rollback_requested -> rolled_back, либо назад в applied при отказе).

    Источник у каждой цели один, поэтому гонка двух исполнителей не пишет дважды.
    decided_at/decided_by не трогаем: это след решения человека, итог раннера — outcome_at.
    Переход вне схемы не пишется (False).
    """
    source = "rollback_requested" if rollback else "approved"
    if not can_transition(source, status):
        return False
    with db.get_connection() as conn, conn.cursor() as cur:
        if rollback:
            # исходный ответ применения сохраняем: по нему видно, что именно было запрещено
            cur.execute(
                "UPDATE module_findings SET status = %s, "
                "response = coalesce(response, '{}'::jsonb) || jsonb_build_object('rollback', %s::jsonb), "
                "error = %s, outcome_at = now() "
                "WHERE id = %s AND status = %s",
                (status, json.dumps({"ok": result.ok, "response": result.response,
                                     "error": result.error}, ensure_ascii=False),
                 result.error, finding_id, source))
        else:
            cur.execute(
                "UPDATE module_findings SET status = %s, response = %s, error = %s, "
                "applied_at = CASE WHEN %s = 'applied' THEN now() ELSE applied_at END, "
                "outcome_at = now() WHERE id = %s AND status = %s",
                (status, _json(result.response), result.error, status, finding_id, source))
        return cur.rowcount == 1


def approve_auto(slug: str, module_key: str, ids: Sequence[int]) -> List[int]:
    """Режим auto: открытые находки с действием сразу одобряются самим раннером.

    decided_at/decided_by не пишем: это след решения человека; итог применения
    запишет set_outcome в outcome_at.
    """
    if not ids:
        return []
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE module_findings SET status = 'approved' "
            "WHERE dashboard_slug = %s AND module_key = %s AND id = ANY(%s) "
            "AND status = 'open' AND action IS NOT NULL RETURNING id",
            (slug, module_key, list(ids)))
        return [r[0] for r in cur.fetchall()]


def request_rollback(slug: str, module_key: str, object_ids: Sequence[str],
                     login: Optional[str] = None) -> int:
    """Откат по списку объектов (путь --unban): применённые -> rollback_requested."""
    if not object_ids:
        return 0
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE module_findings SET status = 'rollback_requested' "
            "WHERE dashboard_slug = %s AND module_key = %s AND status = 'applied' "
            "AND object_id = ANY(%s) AND (%s::text IS NULL OR login = %s)",
            (slug, module_key, list(object_ids), login, login))
        return cur.rowcount


@contextmanager
def hold_project_lock(slug: str) -> Iterator[bool]:
    """Advisory-замок проекта на всё время прогона; False — замок держит другой прогон.

    Замок сессионный: нужно прямое соединение, не пулер в транзакционном режиме (через него
    сессия между запросами меняется и замок молча теряется) — тот же довод, почему аренда
    в sync/agent/writer/db.py:228-233 сделана таблицей. Оборванное соединение замок снимает.
    Соединение выделенное и закрывается только в конце прогона.
    """
    conn = db._open_connection()
    conn.autocommit = True
    got = False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(hashtext('modules:' || %s))", (slug,))
            got = bool(cur.fetchone()[0])
        yield got
    finally:
        try:
            if got:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(hashtext('modules:' || %s))", (slug,))
        finally:
            conn.close()
