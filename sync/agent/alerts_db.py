# -*- coding: utf-8 -*-
"""
sync/agent/alerts_db.py — память сторожа тревог.

Одна таблица, создаётся как остальные таблицы агента (CREATE TABLE IF NOT
EXISTS при первом обращении). Она отвечает на единственный вопрос: говорили
ли уже про это событие сегодня. Без неё почасовой сторож пришлёт восемь
одинаковых сообщений про одну вставшую кампанию, а человек после второго
такого дня перестанет читать все сообщения бота — включая те, что важны.

Отметка ставится ПОСЛЕ успешной отправки. Порядок не косметический: упасть
между записью и отправкой — значит промолчать про событие навсегда, а упасть
между отправкой и записью — прислать одно лишнее сообщение. Второе дешевле.
"""

from typing import Any, Dict, Iterable, List

from sync.db import get_connection

SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS edu_agent_alerts (
      alert_key    TEXT PRIMARY KEY,
      rule         TEXT NOT NULL,
      account      TEXT NOT NULL,
      subject      TEXT NOT NULL,
      day_msk      DATE NOT NULL,
      severity     TEXT NOT NULL,
      text         TEXT NOT NULL,
      evidence     JSONB NOT NULL DEFAULT '{}'::jsonb,
      notified_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS edu_agent_alerts_day_idx
      ON edu_agent_alerts (day_msk DESC);
"""


def ensure_schema() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        conn.commit()


def already_notified(keys: Iterable[str]) -> set:
    """Ключи, про которые уже говорили."""
    keys = [str(k) for k in keys]
    if not keys:
        return set()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT alert_key FROM edu_agent_alerts WHERE alert_key = ANY(%s)",
                (keys,))
            return {row[0] for row in cur.fetchall()}


def mark_notified(alerts: List[Dict[str, Any]], keys: List[str],
                  day_msk: str) -> int:
    """Отметить отправленные. ON CONFLICT DO NOTHING: гонка двух прогонов не
    должна ронять сторожа — второй просто ничего не добавит."""
    import json

    rows = [(key, a["rule"], a["account"], a["subject"], day_msk,
             a["severity"], a["text"],
             json.dumps(a.get("evidence") or {}, ensure_ascii=False))
            for a, key in zip(alerts, keys)]
    if not rows:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO edu_agent_alerts
                  (alert_key, rule, account, subject, day_msk, severity,
                   text, evidence)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (alert_key) DO NOTHING
                """, rows)
        conn.commit()
    return len(rows)
