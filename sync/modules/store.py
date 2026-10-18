# -*- coding: utf-8 -*-
"""Хранилище модулей Директа: project_modules, module_runs, module_findings, module_previews;
чтение целей-лидов из project_sources.

Схему ведут миграции Panda-BI (supabase/migrations/20261018193100_direct_modules.sql,
module_previews — 20261018213700_direct_modules_rules.sql, project_sources —
20261017153700_project_onboarding_core.sql); здесь таблицы не создаются.
"""

import json
from contextlib import contextmanager
from datetime import datetime, timedelta
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


def load_project_modules(slug: str, include_off: bool = False) -> List[dict]:
    """По умолчанию только включённые модули: off раннер не трогает. include_off — для
    репетиции, которая гоняет явно названный модуль с его сохранёнными настройками."""
    with db.get_connection() as conn, _dict_cursor(conn) as cur:
        cur.execute(
            "SELECT module_key, mode, settings, run_requested_at FROM project_modules "
            "WHERE dashboard_slug = %s AND (%s OR mode <> 'off') ORDER BY module_key",
            (slug, include_off))
        return [dict(r) for r in cur.fetchall()]


# Роли целей Метрики, которые считаются лидом (lib/project/source-settings.ts: GoalRole),
# по старшинству. «step» — промежуточный шаг воронки, не лид. quality_lead срабатывает на
# той же заявке, что и lead (EDU quality_lead_ml), — их сумма посчитала бы заявку дважды,
# поэтому качественные цели берутся, только когда целей с ролью lead нет.
LEAD_ROLES = ("lead", "quality_lead")


def project_lead_goals(slug: str) -> List[int]:
    """Цели-лиды проекта из настроек его счётчиков Метрики; нет счётчика — [].

    Счётчиков у проекта может быть несколько: цели всех складываются. Форма settings
    приходит из Panda; разбираем терпимо, кривую цель пропускаем, а не роняем прогон.
    """
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT settings FROM project_sources "
                    "WHERE dashboard_slug = %s AND kind = 'metrika' ORDER BY id", (slug,))
        rows = cur.fetchall()
    by_role = {role: set() for role in LEAD_ROLES}
    for (settings,) in rows:
        goals = settings.get("goals") if isinstance(settings, dict) else None
        for g in goals if isinstance(goals, list) else []:
            if not isinstance(g, dict) or g.get("role") not in LEAD_ROLES:
                continue
            gid = g.get("id")
            if isinstance(gid, int) and not isinstance(gid, bool) and gid > 0:
                by_role[g["role"]].add(gid)
    for role in LEAD_ROLES:
        if by_role[role]:
            return sorted(by_role[role])
    return []


def last_run_started(slug: str, module_key: str,
                     statuses: Optional[Sequence[str]] = None) -> Optional[datetime]:
    """Старт последнего скана; statuses сужает до завершённых этими статусами.

    Строки фазы применения (summary.phase = 'apply') не сканы: иначе применение утром
    закрыло бы суточный скан, а ручной запуск, поставленный до него, потерялся бы.
    """
    st = list(statuses) if statuses else None
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT max(started_at) FROM module_runs "
                    "WHERE dashboard_slug = %s AND module_key = %s "
                    "AND (%s::text[] IS NULL OR status = ANY(%s)) "
                    "AND summary->>'phase' IS DISTINCT FROM 'apply'",
                    (slug, module_key, st, st))
        return cur.fetchone()[0]


def start_run(slug: str, module_key: str, mode: str, phase: Optional[str] = None) -> int:
    """phase пишется в summary сразу: убитая на полпути строка применения не станет «сканом»."""
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO module_runs (dashboard_slug, module_key, mode, summary) "
                    "VALUES (%s, %s, %s, %s) RETURNING id",
                    (slug, module_key, mode, _json({"phase": phase} if phase else {})))
        return cur.fetchone()[0]


def fail_stale_runs(slug: str, older_than: timedelta) -> int:
    """Прогоны проекта, оставшиеся «идёт» дольше older_than, закрыть как прерванные.

    Зовётся под замком проекта; статуса failed в CHECK нет — пишем error.
    """
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE module_runs SET status = 'error', finished_at = now(), "
            "summary = summary || jsonb_build_object('reason', 'прерван'), "
            "error = coalesce(error, 'прогон прерван: не завершился за ' || %s || ' мин') "
            "WHERE dashboard_slug = %s AND status = 'running' "
            "AND started_at < now() - make_interval(secs => %s)",
            (int(older_than.total_seconds() // 60), slug, older_than.total_seconds()))
        return cur.rowcount


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
                # пустая строка — «нет значения»: NULL, иначе подписи-заглушки Panda (??) молчат
                (slug, module_key, run_id, fingerprint(module_key, f), f.rule, f.severity,
                 f.login or None, f.campaign_id, f.campaign_name or None,
                 f.object_type or None, f.object_id, f.title,
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

    Действие, которое человек хоть раз откатил (rolled_back в истории, за любой срок), сам
    раннер больше не одобряет: после окна подавления оно возвращается только предложением,
    и одобрить его может лишь человек. Тождество действия — как lifecycle._action_key.

    Находку правила с действием «предложить» (details.propose) тоже одобряет только человек.
    """
    if not ids:
        return []
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE module_findings f SET status = 'approved' "
            "WHERE f.dashboard_slug = %s AND f.module_key = %s AND f.id = ANY(%s) "
            "AND f.status = 'open' AND f.action IS NOT NULL "
            "AND f.details->>'propose' IS DISTINCT FROM 'true' "
            "AND NOT EXISTS (SELECT 1 FROM module_findings h "
            "  WHERE h.dashboard_slug = f.dashboard_slug AND h.module_key = f.module_key "
            "    AND h.status = 'rolled_back' "
            "    AND coalesce(h.login, '') = coalesce(f.login, '') "
            "    AND h.campaign_id IS NOT DISTINCT FROM f.campaign_id "
            "    AND coalesce(h.object_id, '') = coalesce(f.object_id, '') "
            "    AND h.action->>'op' IS NOT DISTINCT FROM f.action->>'op') "
            "RETURNING f.id",
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


# Превью, которое столько не подавало хода (preview_progress), уже не идёт: процесс убит
# (юнит ограничен 40 минутами, OOM). Без этого следующее превью того же модуля ждало бы
# его вечно. Меряем от последнего хода, а не от старта: большой проект честно идёт дольше.
PREVIEW_STALE_AFTER = timedelta(minutes=20)
PREVIEW_RETENTION = timedelta(days=7)
# Panda узнаёт замену по этим словам (adm-module-rules.ts, previewView).
PREVIEW_REPLACED = "заменено новым"
_PREVIEW_COLS = "id, dashboard_slug, module_key, settings, rule_id, created_at, started_at"


def claim_preview(slug: str, preview_id: Optional[int] = None) -> Optional[dict]:
    """Взять превью проекта в работу (queued -> running) или None, если брать нечего.

    Одна транзакция: зависшие running закрываются как прерванные; из нескольких queued
    одного модуля живо только последнее, старые — error «заменено новым»; берётся старейшее
    из оставшихся у модулей, где ничего не идёт («running дожидается»). SKIP LOCKED: два
    раннера проекта (под замком и без) одну строку не получат оба.

    preview_id — ручной прогон одного превью (modules_run --preview-id): берётся в любом
    статусе, кроме идущего, без замены соседей.
    """
    stale = PREVIEW_STALE_AFTER.total_seconds()
    with db.get_connection() as conn, _dict_cursor(conn) as cur:
        cur.execute(
            "UPDATE module_previews SET status = 'error', finished_at = now(), "
            "error = 'прерван: ' || %s || ' мин без хода' "
            "WHERE dashboard_slug = %s AND status = 'running' "
            "AND coalesce((progress->>'at')::timestamptz, started_at, created_at) "
            "    < now() - make_interval(secs => %s)",
            (int(stale // 60), slug, stale))
        if preview_id is not None:
            cur.execute(
                "UPDATE module_previews SET status = 'running', started_at = now(), "
                "finished_at = NULL, result = NULL, error = NULL, progress = '{}'::jsonb "
                "WHERE id = %s AND dashboard_slug = %s AND status <> 'running' "
                "RETURNING " + _PREVIEW_COLS, (preview_id, slug))
        else:
            cur.execute(
                "UPDATE module_previews p SET status = 'error', finished_at = now(), "
                "error = %s "
                "WHERE p.dashboard_slug = %s AND p.status = 'queued' AND EXISTS ("
                "  SELECT 1 FROM module_previews n WHERE n.dashboard_slug = p.dashboard_slug "
                "    AND n.module_key = p.module_key AND n.status = 'queued' "
                "    AND (n.created_at, n.id) > (p.created_at, p.id))",
                (PREVIEW_REPLACED, slug))
            cur.execute(
                "UPDATE module_previews SET status = 'running', started_at = now() "
                "WHERE status = 'queued' AND id = ("
                "  SELECT q.id FROM module_previews q "
                "  WHERE q.dashboard_slug = %s AND q.status = 'queued' "
                "    AND NOT EXISTS (SELECT 1 FROM module_previews r "
                "      WHERE r.dashboard_slug = q.dashboard_slug "
                "        AND r.module_key = q.module_key AND r.status = 'running') "
                "  ORDER BY q.created_at, q.id LIMIT 1 FOR UPDATE SKIP LOCKED) "
                "RETURNING " + _PREVIEW_COLS, (slug,))
        row = cur.fetchone()
        return dict(row) if row else None


def preview_progress(preview_id: int, progress: dict) -> None:
    """Ход превью для человека; метка at — пульс, по нему claim_preview отличает живое."""
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE module_previews "
                    "SET progress = %s::jsonb || jsonb_build_object('at', now()) "
                    "WHERE id = %s AND status = 'running'", (_json(progress), preview_id))


def finish_preview(preview_id: int, status: str, result: Optional[dict],
                   error: Optional[str] = None) -> bool:
    """Итог превью (done/error). False — строку уже закрыли как прерванную: итог не пишем,
    чтобы не воскрешать превью, которое человек, возможно, уже перезапустил."""
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE module_previews SET status = %s, result = %s, error = %s, "
                    "finished_at = now() WHERE id = %s AND status = 'running'",
                    (status, _json(result) if result is not None else None, error, preview_id))
        return cur.rowcount == 1


def cleanup_previews(slug: str, older_than: timedelta = PREVIEW_RETENTION) -> int:
    """Превью проекта старше older_than удалить; идущее не трогаем."""
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM module_previews WHERE dashboard_slug = %s "
                    "AND status <> 'running' AND created_at < now() - make_interval(secs => %s)",
                    (slug, older_than.total_seconds()))
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
