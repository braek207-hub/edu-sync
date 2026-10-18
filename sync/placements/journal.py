# -*- coding: utf-8 -*-
"""Кэш вердиктов модели для чистильщика площадок.

Журнал запретов переехал в module_findings (sync.modules); здесь остался только
вечный кэш ответов модели: домен не меняет природу, платить за повторный
вопрос о нём не за что.
"""

from typing import List

import psycopg2.extras

from sync.db import enable_rls_for_ddl, get_connection

DDL: List[str] = [
    """
    CREATE TABLE IF NOT EXISTS placement_llm_verdicts (
      placement   TEXT PRIMARY KEY,
      verdict     TEXT NOT NULL,
      why         TEXT,
      model       TEXT,
      decided_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
]


def ensure_tables() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            for statement in DDL:
                cur.execute(statement)
            enable_rls_for_ddl(cur, DDL)
        conn.commit()


def load_llm_verdicts(sites):
    """Вердикты модели из кэша: имя → (вердикт, причина).

    Кэш вечный и в этом весь смысл экономии: домен не меняет природу, и
    платить за повторный вопрос о нём не за что. Пустой список при недоступной
    базе — не ошибка: слой просто спросит модель заново.
    """
    sites = [s for s in {(x or "").strip().lower() for x in sites} if s]
    if not sites:
        return {}
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT placement, verdict, why "
                        "FROM placement_llm_verdicts WHERE placement = ANY(%s)",
                        (sites,))
            return {row[0]: (row[1], row[2] or "") for row in cur.fetchall()}


def save_llm_verdicts(verdicts, model: str) -> None:
    rows = [(site, verdict, why, model)
            for site, (verdict, why) in (verdicts or {}).items()]
    if not rows:
        return
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO placement_llm_verdicts
                  (placement, verdict, why, model)
                VALUES %s
                ON CONFLICT (placement) DO UPDATE
                  SET verdict = EXCLUDED.verdict,
                      why = EXCLUDED.why,
                      model = EXCLUDED.model,
                      decided_at = now()
                """, rows)
        conn.commit()
