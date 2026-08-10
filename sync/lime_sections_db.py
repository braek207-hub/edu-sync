# -*- coding: utf-8 -*-
"""Запись витрин разделов LIME. Идемпотентно: delete по (платформа, день) + insert.

В базе живут только готовые агрегаты — ни логов, ни карт атрибуции. Старая
таблица lime_section_cross_daily (кросс по корзинам источника) дашбордом больше
не читается и синком не пополняется — заморожена как есть.
"""
import psycopg2.extras

from sync.db import get_connection

TABLES = {
    "daily": ("lime_section_daily",
              ["day", "platform", "section", "source", "dau", "views", "cart_users",
               "checkout_users", "buy_users", "orders", "items", "cart_items", "revenue"]),
    "daily_v2": ("lime_section_daily_v2",
                 ["day", "platform", "channel", "device", "section", "dau", "views",
                  "cart_users", "checkout_users"]),
    "product": ("lime_section_product_daily",
                ["day", "platform", "channel", "section", "product_type", "buyers",
                 "orders", "items", "revenue"]),
    "cross_channel": ("lime_section_cross_channel_daily",
                      ["day", "platform", "channel", "visited", "bought", "buyers",
                       "orders", "items", "revenue", "revenue_split"]),
    "campaign": ("lime_section_campaign_daily",
                 ["day", "platform", "campaign", "section", "dau", "cart_users",
                  "buyers", "orders", "items", "revenue"]),
    "campaign_type": ("lime_section_campaign_type_daily",
                      ["day", "platform", "campaign", "section", "product_type",
                       "buyers", "orders", "items", "revenue"]),
    "campaign_cross": ("lime_section_campaign_cross_daily",
                       ["day", "platform", "campaign", "visited", "bought",
                        "buyers", "orders", "items", "revenue", "revenue_split"]),
}


def write_day(platform: str, day: str, sets: dict) -> None:
    """Пишет все таблицы одного дня одной транзакцией: день либо целиком новый,
    либо целиком старый — частично записанных дней не бывает."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            for key, rows in sets.items():
                table, cols = TABLES[key]
                cur.execute(
                    f"delete from {table} where platform = %s and day = %s",  # noqa: S608
                    (platform, day),
                )
                if not rows:
                    continue
                ph = "(" + ",".join(["%s"] * len(cols)) + ")"
                psycopg2.extras.execute_batch(
                    cur,
                    f"insert into {table} ({','.join(cols)}) values {ph} on conflict do nothing",  # noqa: S608
                    rows, page_size=400,
                )
        conn.commit()


def compare_day(platform: str, day: str, sets: dict) -> None:
    """Check-режим: суммы посчитанного дня против сумм в базе, без записи."""
    num_from = {"daily": 4, "daily_v2": 5, "product": 5, "cross_channel": 5, "campaign": 4,
                "campaign_type": 5, "campaign_cross": 5}
    with get_connection() as conn:
        with conn.cursor() as cur:
            for key, rows in sets.items():
                table, cols = TABLES[key]
                nums = cols[num_from[key]:]
                cur.execute(
                    f"select count(*), {', '.join('coalesce(sum(' + c + '),0)' for c in nums)} "  # noqa: S608
                    f"from {table} where platform = %s and day = %s",
                    (platform, day),
                )
                db = cur.fetchone()
                calc = [len(rows)] + [
                    round(sum(float(r[num_from[key] + i] or 0) for r in rows), 2)
                    for i in range(len(nums))
                ]
                db_fmt = [db[0]] + [round(float(x), 2) for x in db[1:]]
                mark = "OK " if all(abs(a - b) < max(0.5, abs(b) * 0.001) for a, b in zip(calc, db_fmt)) else "DIFF"
                print(f"  {mark} {table} [{platform} {day}] строк {calc[0]} vs {db_fmt[0]}")
                for name, a, b in zip(["rows"] + list(nums), calc, db_fmt):
                    if abs(a - b) >= max(0.5, abs(b) * 0.001):
                        print(f"       {name}: посчитано {a} vs база {b}")
