"""Ход прогона синка → public.sync_runs.progress (миграция Panda-BI *_sync_run_progress.sql).

Кнопка «Обновить сейчас» в Panda-BI показывает этап и «X из Y» («Директ · кабинеты 2 из 4»),
а по окончании — что пришло («+30 дн., 1 240 строк»). Синк сообщает о себе сам:

    from sync import progress
    for client in progress.track(clients, "direct.cabinets", "Директ · кабинеты"):
        ...
    progress.report("metrika.days", done=12, total=30, label="Метрика · дни")
    progress.result(rows=written, days=30)

Своя строка sync_runs: её вставляет infra/panda/sync/runlog.sh (ExecStartPre юнита
panda-sync@<экземпляр>) и кладёт «<INVOCATION_ID> <id прогона> …» в /run/panda-sync/<экземпляр>.run.
Синк читает id оттуда и сверяет INVOCATION_ID (systemd ставит его всем процессам запуска) — так
прогресс не попадёт в строку чужого или оборванного прогона. Файла нет или он чужой — запасной
путь: последняя running-строка экземпляра моложе лимита юнита (TimeoutStartSec=30min).

Прогресс — украшение, а не данные: любая ошибка записи глотается с предупреждением в stderr,
после MAX_FAILURES подряд журнал хода выключается до конца процесса. Без PANDA_SYNC_UNIT
(локальный запуск, тесты, GitHub Actions) — ничего не делает и в базу не ходит.
"""

from __future__ import annotations

import atexit
import json
import os
import sys
import time
from typing import Any, Callable, Iterable, Iterator, Optional, TypeVar

UNIT_ENV = "PANDA_SYNC_UNIT"
RUN_DIR_ENV = "PANDA_SYNC_RUN_DIR"
DEFAULT_RUN_DIR = "/run/panda-sync"
# Опрос страницы — раз в 3 с; чаще писать незачем, а строка sync_runs — общая с runlog.sh.
MIN_INTERVAL_S = 2.0
MAX_FAILURES = 3
# Запасной поиск строки: не старше лимита юнита (panda-sync@.service, TimeoutStartSec=30min) с запасом.
FALLBACK_MAX_AGE = "35 minutes"

T = TypeVar("T")

_REPORT_SQL = """
update public.sync_runs
   set progress = coalesce(progress, '{}'::jsonb) || %s::jsonb
 where id = %s and status = 'running'
"""

# rows копятся (юнит из нескольких модулей — bjorn_direct — пишет итог каждым), days — максимум.
_RESULT_SQL = """
update public.sync_runs
   set progress = coalesce(progress, '{}'::jsonb) || jsonb_strip_nulls(jsonb_build_object(
         'rows', case when %(rows)s::bigint is null then null
                      else coalesce((progress->>'rows')::bigint, 0) + %(rows)s::bigint end,
         'days', greatest((progress->>'days')::int, %(days)s::int),
         'updated_at', now()))
 where id = %(id)s and status = 'running'
"""

_FIND_SQL = f"""
select id from public.sync_runs
 where source = %s and stream = 'unit' and status = 'running'
   and started_at > now() - interval '{FALLBACK_MAX_AGE}'
 order by id desc limit 1
"""


def _default_connect():
    from sync.db import _new_connection

    conn = _new_connection(connect_timeout=5)
    conn.autocommit = True
    with conn.cursor() as cur:
        # Запись хода не должна ждать блокировок дольше пары секунд: синк важнее плашки.
        cur.execute("set statement_timeout = 3000")
    return conn


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Reporter:
    """Журнал хода одного процесса. unit=None — выключен (все методы — no-op)."""

    def __init__(
        self,
        unit: Optional[str],
        *,
        connect: Callable[[], Any] = _default_connect,
        clock: Callable[[], float] = time.monotonic,
        run_dir: Optional[str] = None,
        invocation: Optional[str] = None,
    ) -> None:
        self.unit = unit or None
        self._connect = connect
        self._clock = clock
        self._run_dir = run_dir or os.environ.get(RUN_DIR_ENV) or DEFAULT_RUN_DIR
        self._invocation = invocation if invocation is not None else os.environ.get("INVOCATION_ID", "")
        self._conn = None
        self._run_id: Optional[int] = None
        self._resolved = False
        self._failures = 0
        self._last_write = float("-inf")
        self._stage: Optional[str] = None
        self._pending: Optional[dict] = None

    @property
    def enabled(self) -> bool:
        return self.unit is not None and self._failures < MAX_FAILURES

    def report(
        self,
        stage: str,
        done: Optional[int] = None,
        total: Optional[int] = None,
        label: Optional[str] = None,
    ) -> None:
        if not self.enabled:
            return
        payload = {
            "stage": stage,
            "label": label,
            "done": None if done is None else int(done),
            "total": None if total is None else int(total),
        }
        # Новый этап пишется сразу: иначе короткий этап (запись в базу) так и не показался бы.
        if stage != self._stage or self._clock() - self._last_write >= MIN_INTERVAL_S:
            self._stage = stage
            self._write_progress(payload)
        else:
            self._pending = payload

    def flush(self) -> None:
        if self._pending is not None and self.enabled:
            self._write_progress(self._pending)

    def result(self, rows: Optional[int] = None, days: Optional[int] = None) -> None:
        """Итог: сколько строк записано и за сколько дней. Пишется сразу, без троттлинга."""
        if not self.enabled or (rows is None and days is None):
            return
        self.flush()
        self._execute(
            _RESULT_SQL,
            {
                "rows": None if rows is None else int(rows),
                "days": None if days is None else int(days),
                "id": None,
            },
        )

    def track(
        self,
        items: Iterable[T],
        stage: str,
        label: Optional[str] = None,
        total: Optional[int] = None,
    ) -> Iterator[T]:
        """Обёртка цикла: done — сколько элементов уже пройдено, в конце done == total."""
        if total is None:
            try:
                total = len(items)  # type: ignore[arg-type]
            except TypeError:
                total = None
        done = 0
        for item in items:
            self.report(stage, done, total, label)
            yield item
            done += 1
        self.report(stage, done, total if total is not None else done, label)
        self.flush()

    def _write_progress(self, payload: dict) -> None:
        self._pending = None
        self._last_write = self._clock()
        body = dict(payload, updated_at=_now_iso())
        self._execute(_REPORT_SQL, json.dumps(body, ensure_ascii=False))

    def _execute(self, sql: str, arg: Any) -> None:
        try:
            run_id = self._resolve()
            if run_id is None:
                return
            conn = self._conn or self._open()
            with conn.cursor() as cur:
                if isinstance(arg, dict):
                    cur.execute(sql, dict(arg, id=run_id))
                else:
                    cur.execute(sql, (arg, run_id))
            self._failures = 0
        except Exception as e:  # noqa: BLE001 — прогресс не роняет синк
            self._failures += 1
            self._drop_conn()
            off = " — журнал хода выключен" if self._failures >= MAX_FAILURES else ""
            print(f"progress: запись не удалась ({type(e).__name__}: {e}){off}", file=sys.stderr)

    def _open(self):
        self._conn = self._connect()
        return self._conn

    def _drop_conn(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._conn = None

    def _resolve(self) -> Optional[int]:
        if self._resolved:
            return self._run_id
        run_id = self._from_run_file()
        if run_id is None:
            conn = self._conn or self._open()
            with conn.cursor() as cur:
                cur.execute(_FIND_SQL, (self.unit,))
                row = cur.fetchone()
            run_id = int(row[0]) if row else None
        self._run_id = run_id
        self._resolved = True
        if run_id is None:
            print(f"progress: строка прогона {self.unit} не найдена — ход не пишется", file=sys.stderr)
        return run_id

    def _from_run_file(self) -> Optional[int]:
        """«<INVOCATION_ID> <id|-> <эпоха> <заявка|->» — формат runlog.sh (start)."""
        if not self._invocation:
            return None
        try:
            with open(os.path.join(self._run_dir, f"{self.unit}.run"), encoding="utf-8") as f:
                parts = f.read().split()
        except OSError:
            return None
        if len(parts) >= 2 and parts[0] == self._invocation and parts[1].isdigit():
            return int(parts[1])
        return None


_reporter: Optional[Reporter] = None


def _get() -> Reporter:
    global _reporter
    if _reporter is None:
        _reporter = Reporter(os.environ.get(UNIT_ENV))
        if _reporter.unit:
            atexit.register(_reporter.flush)
    return _reporter


def report(stage: str, done: Optional[int] = None, total: Optional[int] = None,
           label: Optional[str] = None) -> None:
    _get().report(stage, done, total, label)


def result(rows: Optional[int] = None, days: Optional[int] = None) -> None:
    _get().result(rows, days)


def track(items: Iterable[T], stage: str, label: Optional[str] = None,
          total: Optional[int] = None) -> Iterator[T]:
    return _get().track(items, stage, label, total)


def flush() -> None:
    _get().flush()
