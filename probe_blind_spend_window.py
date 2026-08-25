# -*- coding: utf-8 -*-
"""
probe_blind_spend_window.py — от какого окна зависит слепая доля расхода.

Боевой прогон Э0 25.08.2026 (run 32859267717) дал слепую долю 30,3 %
(17,85 из 59,0 млн ₽, 82 кампании из 176) — вдвое больше замера 15 %, на
котором строился счётчик. Счётчик считает её за ЗРЕЛОЕ окно лестницы
(18.03–16.06), то есть за период, закончившийся два месяца назад.

Гипотеза: витрину настроек наполняет sync/edu_direct_settings.py, а он берёт
из API кампании в состояниях ON/OFF/SUSPENDED/ENDED — АРХИВНЫХ там нет.
Кампания, отработавшая весной и с тех пор заархивированная, выпадает из
витрины не потому, что агент её не видит, а потому, что её больше нет. Тогда
30,3 % — смесь двух разных величин: настоящей слепой зоны (Мастер кампаний и
прочее вне API) и истории.

Проверяем фактом, а не рассуждением: считаем слепую долю на двух окнах —
зрелом (по нему сейчас печатается число) и трейлинг-28д (по нему принимаются
решения), — и отдельно смотрим, сколько слепых на зрелом окне НЕ тратили
ничего за последние 28 дней. Такие и есть история.

Скрипт read-only: пишет только в stdout.
"""

import json

import psycopg2.extras

from sync.db import get_connection

MATURE_FROM = "2026-03-18"
MATURE_TO = "2026-06-16"


def q(cur, sql, params=()):
    cur.execute(sql, params)
    return cur.fetchall()


def _window_stats(cur, where_sql, params):
    rows = q(cur, f"""
        WITH w AS (
            SELECT campaign_id::text AS campaign_id, SUM(cost) AS cost
            FROM edu_agent_facts
            WHERE {where_sql}
            GROUP BY campaign_id
        )
        SELECT COUNT(*) AS campaigns,
               COALESCE(SUM(cost), 0) AS cost_total,
               COUNT(*) FILTER (WHERE s.campaign_id IS NULL) AS campaigns_blind,
               COALESCE(SUM(w.cost) FILTER (WHERE s.campaign_id IS NULL), 0) AS cost_blind
        FROM w
        LEFT JOIN edu_campaign_settings s ON s.campaign_id::text = w.campaign_id
    """, params)
    row = dict(rows[0])
    total = float(row["cost_total"])
    blind = float(row["cost_blind"])
    return {
        "campaigns": int(row["campaigns"]),
        "campaigns_blind": int(row["campaigns_blind"]),
        "cost_total": round(total, 2),
        "cost_blind": round(blind, 2),
        "blind_share": round(blind / total, 4) if total > 0 else 0.0,
    }


def _report(conn) -> int:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        mature = _window_stats(cur, "fact_date BETWEEN %s AND %s",
                               (MATURE_FROM, MATURE_TO))
        recent = _window_stats(cur, "fact_date >= CURRENT_DATE - 28", ())

        # Слепые зрелого окна, у которых за последние 28 дней нет расхода
        # вовсе: кампания закончилась, и её отсутствие в витрине — не слепота.
        history = q(cur, """
            WITH m AS (
                SELECT campaign_id::text AS campaign_id, SUM(cost) AS cost
                FROM edu_agent_facts
                WHERE fact_date BETWEEN %s AND %s
                GROUP BY campaign_id
            ), r AS (
                SELECT campaign_id::text AS campaign_id, SUM(cost) AS cost
                FROM edu_agent_facts
                WHERE fact_date >= CURRENT_DATE - 28
                GROUP BY campaign_id
            )
            SELECT COUNT(*) AS campaigns,
                   COALESCE(SUM(m.cost), 0) AS cost
            FROM m
            LEFT JOIN edu_campaign_settings s ON s.campaign_id::text = m.campaign_id
            LEFT JOIN r ON r.campaign_id = m.campaign_id
            WHERE s.campaign_id IS NULL AND r.campaign_id IS NULL
        """, (MATURE_FROM, MATURE_TO))
        hist = dict(history[0])

        # Кто именно слеп СЕЙЧАС — по этому списку видно, Мастер кампаний это
        # или что-то другое: имя кампании берём из фактов, витрины у неё нет.
        top_recent_blind = q(cur, """
            SELECT f.campaign_id::text AS campaign_id,
                   MAX(f.campaign_name) AS campaign_name,
                   ROUND(SUM(f.cost)) AS cost
            FROM edu_agent_facts f
            LEFT JOIN edu_campaign_settings s ON s.campaign_id::text = f.campaign_id::text
            WHERE f.fact_date >= CURRENT_DATE - 28 AND s.campaign_id IS NULL
            GROUP BY f.campaign_id
            ORDER BY SUM(f.cost) DESC
            LIMIT 15
        """)

        synced = q(cur, "SELECT MAX(synced_at) AS synced_at, COUNT(*) AS rows FROM edu_campaign_settings")

    print(json.dumps({
        "mature_window": {"from": MATURE_FROM, "to": MATURE_TO, **mature},
        "recent_window_28d": recent,
        "mature_blind_without_recent_spend": {
            "campaigns": int(hist["campaigns"]),
            "cost": round(float(hist["cost"]), 2),
        },
        "settings_snapshot": {
            "rows": int(synced[0]["rows"]),
            "synced_at": str(synced[0]["synced_at"]),
        },
        "recent_blind_top": [
            {"campaign_id": r["campaign_id"],
             "campaign_name": r["campaign_name"],
             "cost": float(r["cost"])}
            for r in top_recent_blind
        ],
    }, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    with get_connection() as conn:
        return _report(conn)


if __name__ == "__main__":
    raise SystemExit(main())
