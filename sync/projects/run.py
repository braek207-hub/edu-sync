# -*- coding: utf-8 -*-
"""Оркестратор синка проектов: ночной перезалив 7 дней и очередь заданий по источникам.

    python -m sync.projects.run daily [--slug <slug>]   # все источники ok/error, окно 7 дней
    python -m sync.projects.run queue [--slug <slug>]   # только строки с project_sources.backfill

Код выхода 0 — все взятые источники прошли, 1 — хоть один упал (юнит поднимет OnFailure).

Транзакции: одна на источник × окно (кусок). access_token и mark_reconnect коммитят сами,
поэтому токен берётся до первой записи окна, а при сбое сначала rollback, потом статус.
Advisory lock источника — сессионный: переживает commit/rollback, снимается в finally.
Бюджет SOURCE_BUDGET_SEC — на каждое окно (кусок истории), а не на источник целиком:
медленная очередь отчётов Директа не должна ронять 90 дней из-за суммы четырёх кусков.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

from sync import db
from sync.projects import accounts, direct_fetch, metrika_fetch, secret_box, store
from sync.projects.store import Source

DAILY_DAYS = 7
BACKFILL_DAYS = 90
SOURCE_BUDGET_SEC = 1200

MSK = timezone(timedelta(hours=3))
METRIKA_401 = "Metrica API error 401"  # префикс RuntimeError из sync/bjorn/common.py _metrica_stat_page
ERROR_MAX = 500

_SECRET_PATTERNS = (
    (re.compile(r"y0_\S+"), "y0_***"),
    (re.compile(r"v1\.[\w.-]+"), "v1.***"),
    (re.compile(r"""(client_secret["']?\s*[:=]\s*["']?)[^"'&\s,}]+"""), r"\1***"),
)
_JOB_DAYS = {"history": BACKFILL_DAYS, "refresh": DAILY_DAYS}


def log(mode: str, message: str) -> None:
    print(f"[projects:{mode}] {message}", flush=True)


def yesterday_msk(now: datetime | None = None) -> date:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(MSK).date() - timedelta(days=1)


def month_chunks(date_from: date, date_to: date) -> list[tuple[date, date]]:
    chunks: list[tuple[date, date]] = []
    start = date_from
    while start <= date_to:
        next_month = (start.replace(day=1) + timedelta(days=32)).replace(day=1)
        end = min(next_month - timedelta(days=1), date_to)
        chunks.append((start, end))
        start = end + timedelta(days=1)
    return chunks


def short_error(exc: BaseException) -> str:
    message = str(exc).strip() or type(exc).__name__
    for pattern, repl in _SECRET_PATTERNS:
        message = pattern.sub(repl, message)
    return message[:ERROR_MAX]


def _is_reconnect(exc: BaseException) -> bool:
    return isinstance(exc, accounts.NeedsReconnect) or str(exc).startswith(METRIKA_401)


def _check_deadline(deadline: float, source: Source) -> None:
    # Предел проверяется между запросами: fetch_* Метрики его не принимают, один запрос может его перейти.
    if time.monotonic() >= deadline:
        raise TimeoutError(f"Окно источника {source.id} не уложилось в {SOURCE_BUDGET_SEC} с")


def _metrika_plan(source: Source):
    """[(таблица, загрузчик)] по настройкам счётчика (ключи — lib/project/source-settings.ts
    в Panda-BI: goals[].id, ecommerce). None — запрос не нужен, окно таблицы стирается."""
    settings = source.settings or {}
    goal_ids = [int(g["id"]) for g in settings.get("goals") or []]
    ecommerce = bool(settings.get("ecommerce"))
    counter = int(source.external_id)
    return [
        ("project_metrika_traffic", lambda tok, f, t: metrika_fetch.fetch_traffic(counter, tok, f, t)),
        (
            "project_metrika_goals",
            (lambda tok, f, t: metrika_fetch.fetch_goals(counter, tok, f, t, goal_ids)) if goal_ids else None,
        ),
        (
            "project_metrika_purchases",
            (lambda tok, f, t: metrika_fetch.fetch_purchases(counter, tok, f, t)) if ecommerce else None,
        ),
        (
            "project_metrika_products",
            (lambda tok, f, t: metrika_fetch.fetch_products(counter, tok, f, t)) if ecommerce else None,
        ),
    ]


def sync_source(conn, source: Source, date_from: str, date_to: str, *, key_b64: str, deadline: float) -> int:
    """Перезаливает окно источника; записи окна не коммитит. Сначала токен и коммит, затем все
    запросы к Яндексу, потом записи — транзакция не висит открытой на время сети."""
    _check_deadline(deadline, source)
    token = accounts.access_token(conn, source.account_id, key_b64=key_b64)
    # access_token без обновления только читает и не коммитит: закрыть транзакцию SELECT'ов
    # (замок, перечитывание, токен), чтобы на время сети не висел idle in transaction.
    conn.commit()

    if source.kind == "direct":
        _check_deadline(deadline, source)
        rows = direct_fetch.fetch_direct_daily(
            source.external_id, token, date_from, date_to, max_wait_sec=max(1.0, deadline - time.monotonic())
        )
        return store.replace_window(conn, "project_direct_daily", source, date_from, date_to, rows)

    if source.kind != "metrika":
        raise ValueError(f"Неизвестный тип источника: {source.kind}")

    fetched: list[tuple[str, list]] = []
    for table, load in _metrika_plan(source):
        if load is None:
            # Цели или e-com выключены в настройках: старые строки в окне не должны остаться.
            fetched.append((table, []))
            continue
        _check_deadline(deadline, source)
        fetched.append((table, load(token, date_from, date_to)))

    written = 0
    for table, rows in fetched:
        if table == "project_metrika_purchases":
            written += store.replace_purchases(conn, source, date_from, date_to, rows)
        else:
            written += store.replace_window(conn, table, source, date_from, date_to, rows)
    return written


def _record_failure(conn, mode: str, source: Source, exc: Exception) -> None:
    """Вызывать после rollback (и после записи backfill=failed, если она есть): mark_reconnect
    коммитит всё незакоммиченное на conn, mark_error — коммитим здесь."""
    message = short_error(exc)
    if _is_reconnect(exc):
        log(mode, f"источник {source.id} ({source.slug}/{source.kind}): нужно переподключение — {message}")
        accounts.mark_reconnect(conn, source.account_id, message)
    else:
        log(mode, f"источник {source.id} ({source.slug}/{source.kind}): ОШИБКА {message}")
        store.mark_error(conn, source.id, message)
        conn.commit()


def run_daily(conn, *, today_msk: date, key_b64: str, slug: str | None = None) -> int:
    date_to = today_msk - timedelta(days=1)
    date_from = date_to - timedelta(days=DAILY_DAYS - 1)
    window = (date_from.isoformat(), date_to.isoformat())
    failed = 0
    for source in store.load_daily_sources(conn, slug):
        if not store.try_lock_source(conn, source.id):
            log("daily", f"источник {source.id} занят, пропуск")
            continue
        try:
            # Под замком перечитать: задание могли поставить после чтения списка. Его окно
            # отработает очередь (история перекрывает ночные 7 дней) — ночной прогон не пишет поверх.
            if any(s.id == source.id for s in store.load_queued_sources(conn, source.slug)):
                log("daily", f"источник {source.id}: есть задание в очереди, пропуск")
                continue
            deadline = time.monotonic() + SOURCE_BUDGET_SEC
            try:
                n = sync_source(conn, source, *window, key_b64=key_b64, deadline=deadline)
                store.mark_ok(conn, source.id)
                conn.commit()
                log("daily", f"источник {source.id} ({source.slug}/{source.kind}) {window[0]}..{window[1]}: {n} строк")
            except Exception as exc:  # noqa: BLE001 — сбой одного источника не роняет остальные
                conn.rollback()
                _record_failure(conn, "daily", source, exc)
                failed += 1
        finally:
            store.unlock_source(conn, source.id)
            conn.commit()
    return 1 if failed else 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_kind(job: dict) -> str:
    kind = job.get("kind")
    if kind is None:
        raise ValueError("задание без kind")
    if kind not in _JOB_DAYS:
        raise ValueError(f"Неизвестный вид задания: {kind}")
    return kind


def _job_chunks(job: dict) -> list[tuple[date, date]]:
    kind = _job_kind(job)
    if not job.get("from") or not job.get("to"):
        raise ValueError("задание без границ from/to")
    date_from, date_to = date.fromisoformat(job["from"]), date.fromisoformat(job["to"])
    return month_chunks(date_from, date_to) if kind == "history" else [(date_from, date_to)]


def _start_job(conn, source: Source, job: dict, yesterday: date) -> dict:
    kind = _job_kind(job)
    days = job.get("days", _JOB_DAYS[kind])
    if not isinstance(days, int) or isinstance(days, bool) or days < 1:
        raise ValueError(f"Некорректная глубина задания: {days}")
    job = {k: v for k, v in job.items() if k not in ("error", "finished_at")}
    job.update(state="running", kind=kind, days=days, chunks_done=0, started_at=_now_iso())
    job["from"] = (yesterday - timedelta(days=days - 1)).isoformat()
    job["to"] = yesterday.isoformat()
    job["chunks_total"] = len(_job_chunks(job))
    store.save_backfill(conn, source.id, job, status="backfilling" if kind == "history" else None)
    conn.commit()
    return job


def _run_job(conn, source: Source, *, yesterday: date, key_b64: str) -> bool:
    job = dict(source.backfill or {})
    try:
        if job.get("state") == "queued":
            job = _start_job(conn, source, job, yesterday)
        chunks = _job_chunks(job)
        for i in range(int(job.get("chunks_done", 0)), len(chunks)):
            chunk_from, chunk_to = (d.isoformat() for d in chunks[i])
            deadline = time.monotonic() + SOURCE_BUDGET_SEC
            n = sync_source(conn, source, chunk_from, chunk_to, key_b64=key_b64, deadline=deadline)
            job["chunks_done"] = i + 1
            store.save_backfill(conn, source.id, job)
            conn.commit()
            log(
                "queue",
                f"источник {source.id} ({source.slug}/{source.kind}) {chunk_from}..{chunk_to}: "
                f"{n} строк, кусок {i + 1}/{len(chunks)}",
            )
        job.update(state="done", finished_at=_now_iso())
        store.save_backfill(conn, source.id, job)
        store.mark_ok(conn, source.id)
        conn.commit()
        return True
    except Exception as exc:  # noqa: BLE001 — упавшее задание не роняет очередь
        conn.rollback()
        job.update(state="failed", error=short_error(exc), finished_at=_now_iso())
        store.save_backfill(conn, source.id, job)
        _record_failure(conn, "queue", source, exc)
        return False


def run_queue(conn, *, today_msk: date, key_b64: str, slug: str | None = None) -> int:
    yesterday = today_msk - timedelta(days=1)
    failed_ids: set[int] = set()
    busy_ids: set[int] = set()
    while True:
        # Очередь перечитывается после каждого источника: задания, пришедшие во время прогона,
        # тоже отработают. Упавшие и занятые в этом прогоне не берутся — цикл конечен.
        pending = [s for s in store.load_queued_sources(conn, slug) if s.id not in failed_ids | busy_ids]
        if not pending:
            break
        source_id = pending[0].id
        if not store.try_lock_source(conn, source_id):
            log("queue", f"источник {source_id} занят, задание остаётся в очереди")
            busy_ids.add(source_id)
            continue
        try:
            # Под замком перечитать: задание могло уйти (другой исполнитель) или смениться.
            source = next((s for s in store.load_queued_sources(conn, slug) if s.id == source_id), None)
            if source is not None and not _run_job(conn, source, yesterday=yesterday, key_b64=key_b64):
                failed_ids.add(source_id)
        finally:
            store.unlock_source(conn, source_id)
            conn.commit()
    return 1 if failed_ids else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Синк источников проектов: Директ и Метрика.")
    parser.add_argument("mode", choices=("daily", "queue"))
    parser.add_argument("--slug", default=None, help="только источники этого проекта")
    args = parser.parse_args(argv)

    try:
        key_b64 = secret_box.key_from_env()
        # Пробное шифрование проверяет ключ: битый ключ — ошибка конфига сервера, а не всех источников.
        secret_box.encrypt("", key_b64)
    except secret_box.SecretBoxError as exc:
        print(f"ERROR: {exc}")
        return 1

    print(f"=== projects {args.mode} START ===", flush=True)
    today_msk = yesterday_msk() + timedelta(days=1)
    runner = run_daily if args.mode == "daily" else run_queue
    with db.get_connection() as conn:
        code = runner(conn, today_msk=today_msk, key_b64=key_b64, slug=args.slug)
    print(f"=== projects {args.mode} {'OK' if code == 0 else 'FAILED'} ===", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
