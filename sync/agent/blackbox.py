# -*- coding: utf-8 -*-
"""
sync/agent/blackbox.py — чёрный ящик прогонов агента.

Отчёт прогона — единственное место, где записано, ЧТО агент решил и почему.
До этого модуля он существовал одним JSON-ом в логе GitHub Actions: логи
живут ограниченный срок, не запрашиваются и не сравниваются между собой.
Через две недели боя вопрос «почему он тогда так сделал» ответа не имел.

Бета — это период, когда такие вопросы задают каждый день, поэтому отчёт
кладётся в базу целиком, вместе с версией кода и ссылкой на прогон: разбор
инцидента начинается с сопоставления двух прогонов, а не с восстановления
обстоятельств по памяти.

Второе, что здесь хранится, — ОТКАЗЫ (sync/agent/rejects.py). Журнал
edu_agent_actions знает только применённое; всё, что агент хотел и не смог,
жило счётчиками в отчёте. А логические нестыковки видны именно в отказах:
одно и то же намерение, повторяющееся день за днём и каждый раз упирающееся
в одну и ту же стену, — это не случайность прогона, а дефект подхода.

Запись чёрного ящика никогда не роняет прогон: наблюдение не имеет права
стоить дороже наблюдаемого. Сбой записи возвращается вызывающему как поле
отчёта, а не как исключение.
"""

import json
import os
import uuid
from typing import Any, Dict, List, Optional

import psycopg2.extras

from sync.db import enable_rls_for_ddl, get_connection

# Ставится в mode, когда прогон ничего не пишет в кабинет. Три режима, а не
# флаг «боевой»: репетиция по боевому кабинету и прогон по песочнице
# отличаются данными, на которых считался план, и сравнивать их между собой
# как одно и то же — ошибка разбора.
MODE_APPLY = "apply"
MODE_REHEARSAL = "rehearsal"
MODE_SANDBOX = "sandbox"
# Такт расчёта в кабинет не пишет вовсе — у него нет «боевого» варианта, и
# звать его репетицией неверно: репетиция это то, что могло бы примениться.
MODE_COMPUTE = "compute"

BLACKBOX_DDL: List[str] = [
    """
    CREATE TABLE IF NOT EXISTS edu_agent_runs (
      run_id       TEXT PRIMARY KEY,
      stage        TEXT NOT NULL,
      mode         TEXT NOT NULL,
      started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
      code_sha     TEXT,
      run_url      TEXT,
      verdict      TEXT,
      report       JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    # Индекс по стадии и времени: типовой запрос разбора — «покажи последние
    # прогоны e1» и «сравни сегодняшний с позавчерашним».
    """
    CREATE INDEX IF NOT EXISTS edu_agent_runs_stage_time
      ON edu_agent_runs (stage, started_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS edu_agent_rejects (
      reject_id    BIGSERIAL PRIMARY KEY,
      run_id       TEXT NOT NULL,
      stage        TEXT NOT NULL,
      account      TEXT NOT NULL DEFAULT '',
      object_id    TEXT NOT NULL DEFAULT '',
      kind         TEXT NOT NULL DEFAULT '',
      key          TEXT NOT NULL DEFAULT '',
      reason       TEXT NOT NULL,
      cost_rub     DOUBLE PRECISION NOT NULL DEFAULT 0,
      risk_rub     DOUBLE PRECISION NOT NULL DEFAULT 0,
      detail       JSONB NOT NULL DEFAULT '{}'::jsonb,
      created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # Главный запрос беты — «что упирается в одну и ту же стену изо дня в
    # день»: группировка по причине и объекту за период.
    """
    CREATE INDEX IF NOT EXISTS edu_agent_rejects_reason_time
      ON edu_agent_rejects (reason, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS edu_agent_rejects_run
      ON edu_agent_rejects (run_id)
    """,
]

INSERT_RUN_SQL = """
INSERT INTO edu_agent_runs (run_id, stage, mode, code_sha, run_url, verdict, report)
VALUES (%(run_id)s, %(stage)s, %(mode)s, %(code_sha)s, %(run_url)s,
        %(verdict)s, %(report)s::jsonb)
ON CONFLICT (run_id) DO NOTHING
"""

INSERT_REJECT_SQL = """
INSERT INTO edu_agent_rejects
  (run_id, stage, account, object_id, kind, key, reason, cost_rub, risk_rub, detail)
VALUES
  (%(run_id)s, %(stage)s, %(account)s, %(object_id)s, %(kind)s, %(key)s,
   %(reason)s, %(cost_rub)s, %(risk_rub)s, %(detail)s::jsonb)
"""


def ensure_blackbox_tables() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            for statement in BLACKBOX_DDL:
                cur.execute(statement)
            enable_rls_for_ddl(cur, BLACKBOX_DDL)
        conn.commit()


def new_run_id() -> str:
    return uuid.uuid4().hex


def run_mode(sandbox: bool, dry_run: bool) -> str:
    if sandbox:
        return MODE_SANDBOX
    return MODE_REHEARSAL if dry_run else MODE_APPLY


def run_context() -> Dict[str, Optional[str]]:
    """Версия кода и ссылка на прогон — из окружения Actions.

    Без версии кода отчёт нельзя сопоставить с поведением: «агент вчера
    решал иначе» и «вчера был другой код» — разные новости, а по одному
    только отчёту они выглядят одинаково. Локальный запуск даёт пустые
    значения, и это честнее выдуманной ссылки.
    """
    sha = os.environ.get("GITHUB_SHA") or None
    server = os.environ.get("GITHUB_SERVER_URL") or "https://github.com"
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    url = f"{server}/{repo}/actions/runs/{run_id}" if repo and run_id else None
    return {"code_sha": sha, "run_url": url}


def save_run(run_id: str, stage: str, mode: str, report: Dict[str, Any],
             rejects: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Кладёт отчёт и отказы в базу. Возвращает итог записи, а не бросает.

    Наблюдение не имеет права стоить дороже наблюдаемого: прогон, который
    применил действия в кабинет и упал на записи собственного отчёта, — это
    потерянный журнал при живых изменениях, то есть худшее из состояний.
    Поэтому любая ошибка возвращается полем и едет в отчёт прогона.
    """
    out: Dict[str, Any] = {"run_id": run_id, "saved": False,
                           "rejects": 0, "error": None}
    context = run_context()
    rows = list(rejects or [])
    try:
        ensure_blackbox_tables()
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(INSERT_RUN_SQL, {
                    "run_id": run_id,
                    "stage": stage,
                    "mode": mode,
                    "code_sha": context["code_sha"],
                    "run_url": context["run_url"],
                    "verdict": str(report.get("verdict") or "")[:200] or None,
                    "report": json.dumps(report, ensure_ascii=False, default=str),
                })
                if rows:
                    psycopg2.extras.execute_batch(cur, INSERT_REJECT_SQL, [
                        {**row,
                         "run_id": run_id,
                         "stage": row.get("stage") or stage,
                         "detail": json.dumps(row.get("detail") or {},
                                              ensure_ascii=False, default=str)}
                        for row in rows
                    ], page_size=500)
            conn.commit()
    except Exception as exc:                            # noqa: BLE001 — см. докстринг
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return out
    out["saved"] = True
    out["rejects"] = len(rows)
    return out
