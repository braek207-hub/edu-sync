# -*- coding: utf-8 -*-
"""Журнал чистильщика: что и почему было запрещено.

Без журнала бот, ходящий по шести кабинетам каждый час, — чёрный ящик:
через неделю нельзя ни объяснить срез, ни откатить его, ни увидеть, что
правило начало резать живое. Пишем и запреты, и сводку такта, включая
холостые: отсутствие строки должно означать «прогон не состоялся», а не
«резать было нечего».
"""

import json
from typing import Any, Dict, List

import psycopg2.extras

from sync.db import enable_rls_for_ddl, get_connection

DDL: List[str] = [
    """
    CREATE TABLE IF NOT EXISTS placement_cleaner_runs (
      run_id        BIGSERIAL PRIMARY KEY,
      started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
      account       TEXT NOT NULL,
      login         TEXT NOT NULL,
      dry_run       BOOLEAN NOT NULL DEFAULT FALSE,
      day_sites     INTEGER NOT NULL DEFAULT 0,
      day_clicks    INTEGER NOT NULL DEFAULT 0,
      day_cost      DOUBLE PRECISION NOT NULL DEFAULT 0,
      campaigns_hit INTEGER NOT NULL DEFAULT 0,
      sites_cut     INTEGER NOT NULL DEFAULT 0,
      cut_clicks    INTEGER NOT NULL DEFAULT 0,
      cut_cost      DOUBLE PRECISION NOT NULL DEFAULT 0,
      summary       JSONB,
      refused       JSONB,
      error         TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS placement_cleaner_cuts (
      run_id        BIGINT NOT NULL,
      cut_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
      account       TEXT NOT NULL,
      login         TEXT NOT NULL,
      campaign_id   TEXT NOT NULL,
      campaign_name TEXT,
      placement     TEXT NOT NULL,
      verdict       TEXT NOT NULL,
      reason        TEXT,
      clicks        INTEGER NOT NULL DEFAULT 0,
      cost          DOUBLE PRECISION NOT NULL DEFAULT 0,
      fill_after    INTEGER NOT NULL DEFAULT 0,
      applied       BOOLEAN NOT NULL DEFAULT FALSE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS placement_llm_verdicts (
      placement   TEXT PRIMARY KEY,
      verdict     TEXT NOT NULL,
      why         TEXT,
      model       TEXT,
      decided_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS placement_cleaner_cuts_run_idx
      ON placement_cleaner_cuts (run_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS placement_cleaner_cuts_site_idx
      ON placement_cleaner_cuts (placement)
    """,
]


def ensure_tables() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            for statement in DDL:
                cur.execute(statement)
            enable_rls_for_ddl(cur, DDL)
        conn.commit()


def start_run(account: str, login: str, dry_run: bool,
              plan: Dict[str, Any]) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO placement_cleaner_runs
                  (account, login, dry_run, day_sites, day_clicks, day_cost,
                   campaigns_hit, sites_cut, cut_clicks, cut_cost,
                   summary, refused)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING run_id
                """,
                (account, login, dry_run,
                 plan.get("day_sites", 0), plan.get("day_clicks", 0),
                 plan.get("day_cost", 0.0),
                 len(plan.get("actions") or []),
                 sum(len(a["added"]) for a in plan.get("actions") or []),
                 sum(a["cut_clicks"] for a in plan.get("actions") or []),
                 sum(a["cut_cost"] for a in plan.get("actions") or []),
                 json.dumps(plan.get("summary") or {}, ensure_ascii=False),
                 json.dumps(plan.get("refused") or [], ensure_ascii=False)))
            run_id = cur.fetchone()[0]
        conn.commit()
    return run_id


def record_cuts(run_id: int, account: str, login: str,
                action: Dict[str, Any], applied: bool) -> None:
    rows = [(run_id, account, login, action["campaign_id"],
             action.get("campaign_name"), a["placement"], a["verdict"],
             a["reason"], a["clicks"], a["cost"], action["fill_after"], applied)
            for a in action["added"]]
    if not rows:
        return
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO placement_cleaner_cuts
                  (run_id, account, login, campaign_id, campaign_name,
                   placement, verdict, reason, clicks, cost, fill_after,
                   applied)
                VALUES %s
                """, rows)
        conn.commit()


def fail_run(run_id: int, error: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE placement_cleaner_runs SET error=%s "
                        "WHERE run_id=%s", (error[:2000], run_id))
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
