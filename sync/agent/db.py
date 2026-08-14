# -*- coding: utf-8 -*-
"""
sync/agent/db.py — схема и доступ к БД автопилота Директа.

Все таблицы автопилота имеют префикс edu_agent_ и не пересекаются с существующими.
DDL идемпотентен: ensure_agent_tables() безопасно вызывать на каждом прогоне
(тот же приём, что sync/db.py::ensure_schema).

Бюджет объёма — ориентир ~40 МБ на все таблицы Э0. Держится тремя решениями:
срезы недельные и в трёх двумерных разрезах (не декартово произведение);
структура и настройки версионируются по content_hash (новая строка только при
изменении); поисковые запросы — агрегат за окно без дат и только с кликами.
"""

import json
from typing import Any, Dict, List

import psycopg2.extras

from sync.db import get_connection

AGENT_DDL: List[str] = [
    # Ежедневный снимок фактов: грань campaign × date, как расход Директа.
    """
    CREATE TABLE IF NOT EXISTS edu_agent_facts (
      fact_date        DATE NOT NULL,
      campaign_id      TEXT NOT NULL,
      campaign_name    TEXT,
      project          TEXT,
      direction        TEXT,
      cost             DOUBLE PRECISION NOT NULL DEFAULT 0,
      clicks           INTEGER NOT NULL DEFAULT 0,
      impressions      INTEGER NOT NULL DEFAULT 0,
      leads            INTEGER NOT NULL DEFAULT 0,
      eff_leads        INTEGER NOT NULL DEFAULT 0,
      sum_p_pay        DOUBLE PRECISION NOT NULL DEFAULT 0,
      payments_fact    INTEGER NOT NULL DEFAULT 0,
      avg_impr_pos     DOUBLE PRECISION NOT NULL DEFAULT 0,
      auction_win_share DOUBLE PRECISION NOT NULL DEFAULT 0,
      connected_leads          INTEGER NOT NULL DEFAULT 0,
      deals                    INTEGER NOT NULL DEFAULT 0,
      mins_to_connection_sum   DOUBLE PRECISION NOT NULL DEFAULT 0,
      mins_to_connection_count INTEGER NOT NULL DEFAULT 0,
      days_to_pay_sum          DOUBLE PRECISION NOT NULL DEFAULT 0,
      days_to_pay_count        INTEGER NOT NULL DEFAULT 0,
      revenue                  DOUBLE PRECISION NOT NULL DEFAULT 0,
      collected_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
      PRIMARY KEY (fact_date, campaign_id)
    )
    """,
    # Срезы: НЕДЕЛЬНАЯ грань и три отдельных двумерных разреза вместо декартова
    # произведения. slice_kind ∈ region|network|device|gender|age|hour.
    # Хвост (мало кликов) сворачивается в slice_key='other' — иначе длинный хвост
    # регионов раздувает таблицу и ничего не добавляет: сжатие всё равно обнулит
    # корректировку на малом объёме.
    """
    CREATE TABLE IF NOT EXISTS edu_agent_facts_sliced (
      week_start    DATE NOT NULL,
      campaign_id   TEXT NOT NULL,
      slice_kind    TEXT NOT NULL,
      slice_key     TEXT NOT NULL,
      cost          DOUBLE PRECISION NOT NULL DEFAULT 0,
      clicks        INTEGER NOT NULL DEFAULT 0,
      impressions   INTEGER NOT NULL DEFAULT 0,
      conversions   INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (week_start, campaign_id, slice_kind, slice_key)
    )
    """,
    # Структура кабинета: группы, фразы, объявления. Новая версия пишется ТОЛЬКО
    # при изменении содержимого (content_hash), а не ежедневно.
    """
    CREATE TABLE IF NOT EXISTS edu_agent_objects (
      object_level  TEXT NOT NULL,
      object_id     TEXT NOT NULL,
      parent_id     TEXT,
      campaign_id   TEXT NOT NULL,
      content_hash  TEXT NOT NULL,
      payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
      first_seen    DATE NOT NULL,
      last_seen     DATE NOT NULL,
      PRIMARY KEY (object_level, object_id, content_hash)
    )
    """,
    # Поисковые запросы: агрегат за окно без дат, только с кликами.
    """
    CREATE TABLE IF NOT EXISTS edu_agent_search_queries (
      window_from   DATE NOT NULL,
      window_to     DATE NOT NULL,
      campaign_id   TEXT NOT NULL,
      query         TEXT NOT NULL,
      matched_key   TEXT,
      cost          DOUBLE PRECISION NOT NULL DEFAULT 0,
      clicks        INTEGER NOT NULL DEFAULT 0,
      conversions   INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (window_from, campaign_id, query)
    )
    """,
    # Полный снимок настроек кампании. Версионируется хешем, как и структура.
    """
    CREATE TABLE IF NOT EXISTS edu_agent_settings_snapshot (
      campaign_id   TEXT NOT NULL,
      content_hash  TEXT NOT NULL,
      settings      JSONB NOT NULL DEFAULT '{}'::jsonb,
      first_seen    DATE NOT NULL,
      last_seen     DATE NOT NULL,
      PRIMARY KEY (campaign_id, content_hash)
    )
    """,
    # Журнал гейта качества данных: почему агент работал или спал.
    """
    CREATE TABLE IF NOT EXISTS edu_agent_guard (
      run_ts       TIMESTAMPTZ NOT NULL DEFAULT now(),
      check_name   TEXT NOT NULL,
      status       TEXT NOT NULL,
      detail       JSONB NOT NULL DEFAULT '{}'::jsonb,
      PRIMARY KEY (run_ts, check_name)
    )
    """,
    # Заповедник: кампании, которых агент не касается.
    """
    CREATE TABLE IF NOT EXISTS edu_agent_holdout (
      campaign_id   TEXT PRIMARY KEY,
      direction     TEXT,
      stratum       TEXT NOT NULL,
      included_at   DATE NOT NULL,
      excluded_at   DATE,
      reason        TEXT NOT NULL
    )
    """,
    # Блокнот: единственный носитель накопленного опыта.
    """
    CREATE TABLE IF NOT EXISTS edu_agent_experiments (
      experiment_id     TEXT PRIMARY KEY,
      hypothesis_type   TEXT NOT NULL,
      object_level      TEXT NOT NULL,
      object_id         TEXT NOT NULL,
      params            JSONB NOT NULL DEFAULT '{}'::jsonb,
      mechanism         TEXT NOT NULL,
      started_on        DATE,
      measured_on       DATE,
      effect            DOUBLE PRECISION,
      effect_lo         DOUBLE PRECISION,
      effect_hi         DOUBLE PRECISION,
      metric            TEXT NOT NULL,
      verdict           TEXT,
      reliability_class TEXT NOT NULL,
      source            TEXT NOT NULL,
      created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # Вычисляемые настройки: посчитаны на Э0, применяются в Э1.
    """
    CREATE TABLE IF NOT EXISTS edu_agent_computed_settings (
      calc_date     DATE NOT NULL,
      object_level  TEXT NOT NULL,
      object_id     TEXT NOT NULL,
      setting_kind  TEXT NOT NULL,
      setting_key   TEXT NOT NULL,
      value         DOUBLE PRECISION NOT NULL,
      support_n     INTEGER NOT NULL,
      raw_value     DOUBLE PRECISION NOT NULL,
      PRIMARY KEY (calc_date, object_level, object_id, setting_kind, setting_key)
    )
    """,
    # Профиль успеха и дистанция каждой кампании до него.
    """
    CREATE TABLE IF NOT EXISTS edu_agent_profile (
      calc_date    DATE NOT NULL,
      campaign_id  TEXT NOT NULL,
      distance     DOUBLE PRECISION NOT NULL,
      gaps         JSONB NOT NULL DEFAULT '[]'::jsonb,
      quartile     INTEGER NOT NULL,
      PRIMARY KEY (calc_date, campaign_id)
    )
    """,
]


def ensure_agent_tables() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            for statement in AGENT_DDL:
                cur.execute(statement)
        conn.commit()
