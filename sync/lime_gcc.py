# -*- coding: utf-8 -*-
"""sync/lime_gcc.py — оркестратор GCC: Метрика(трафик) + Triple Whale(заказы/расход) → lime_stats (region='gcc').

Мержит три источника по ключу (channel, subchannel) в единую строку `lime_stats`:
- sync.gcc_metrika.fetch_metrika_traffic — визиты/юзеры (Яндекс.Метрика, счётчик GCC)
- sync.gcc_triplewhale.aggregate_orders_by_channel — заказы/выручка (TW attribution, AED)
- sync.gcc_triplewhale.spend_by_channel — расход (TW summary-page, AED)
Деньги (cost/revenue) конвертируются AED→RUB по курсу ЦБ (sync.fx.to_rub); трафик — как есть.

Каналы каждого источника уже приведены к единой таксономии (sync.gcc_channels): Метрика через
map_metrika_channel, TW-заказы/расход через map_tw_source/SPEND_METRIC_MAP — поэтому мерж по
(channel, subchannel) валиден.

customers/new_users/new_customers/new_customers_revenue = 0: TW-атрибуция не даёт чистого
per-channel деления новый/лояльный клиент — блок «Новые/Лояльные» GCC пуст в v1.

ENV: GCC_METRICA_TOKEN, GCC_METRICA_COUNTER_ID (default 98232701), GCC_TRIPLEWHALE_API_KEY,
GCC_TW_SHOP_DOMAIN, DATABASE_URL, LIME_GCC_SYNC_FROM/LIME_GCC_SYNC_TO или LIME_GCC_SYNC_DAYS
(default 7). LIME_GCC_DRY_RUN — пропустить БД, только напечатать сводку (для локальной проверки
без доступа к прод-Supabase с машины).
Запуск: python -m sync.lime_gcc
"""
import os
from datetime import date, timedelta

import psycopg2
import psycopg2.extras

from sync.fx import to_rub as fx_to_rub
from sync.gcc_channels import map_metrika_channel
from sync.gcc_metrika import fetch_metrika_traffic
from sync.gcc_triplewhale import aggregate_orders_by_channel, fetch_tw_orders, fetch_tw_spend, spend_by_channel

METRICA_COUNTER_ID = os.environ.get("GCC_METRICA_COUNTER_ID") or "98232701"
SYNC_DAYS = int(os.environ.get("LIME_GCC_SYNC_DAYS") or "7")

DELETE_SQL = "DELETE FROM lime_stats WHERE region = 'gcc' AND date >= %s AND date <= %s"

INSERT_SQL = """
INSERT INTO lime_stats (
    date, data_source, region, channel, subchannel, traffic_type,
    campaign_id, campaign_name,
    cost, clicks, impressions, sessions, users, clients,
    purchases_count, purchases_revenue, customers,
    new_users, new_customers, new_customers_revenue
) VALUES %s
"""


def merge_rows(metrika_rows, tw_order_rows, tw_spend_rows, fx_rate, date_s) -> list[tuple]:
    """Свернуть трафик (Метрика) + заказы/расход (Triple Whale) по (channel, subchannel).

    Args:
        metrika_rows: sync.gcc_metrika.fetch_metrika_traffic() за день date_s.
        tw_order_rows: sync.gcc_triplewhale.aggregate_orders_by_channel() за день date_s.
        tw_spend_rows: sync.gcc_triplewhale.spend_by_channel() за день date_s.
        fx_rate: курс AED→RUB (sync.fx.to_rub("AED", date_s)).
        date_s: дата строк (YYYY-MM-DD).

    Returns:
        Список кортежей в порядке колонок INSERT_SQL, по одному на (channel, subchannel).
    """
    agg: dict[tuple[str, str], dict] = {}

    def _bucket(channel, subchannel, traffic_type):
        key = (channel, subchannel)
        row = agg.get(key)
        if row is None:
            row = {
                "traffic_type": traffic_type,
                "sessions": 0,
                "users": 0,
                "orders": 0,
                "revenue": 0.0,
                "cost": 0.0,
            }
            agg[key] = row
        elif not row["traffic_type"]:
            row["traffic_type"] = traffic_type
        return row

    for m in metrika_rows:
        channel, subchannel, traffic_type = map_metrika_channel(m["traffic_source"], m["source_engine"])
        row = _bucket(channel, subchannel, traffic_type)
        row["sessions"] += int(m["visits"] or 0)
        row["users"] += int(m["users"] or 0)

    for o in tw_order_rows:
        row = _bucket(o["channel"], o["subchannel"], o.get("traffic_type"))
        row["orders"] += int(o["orders"] or 0)
        row["revenue"] += float(o["revenue"] or 0)

    for sp in tw_spend_rows:
        row = _bucket(sp["channel"], sp["subchannel"], sp.get("traffic_type"))
        row["cost"] += float(sp["cost"] or 0)

    out: list[tuple] = []
    for (channel, subchannel), row in agg.items():
        cost_rub = round(row["cost"] * fx_rate, 2)
        revenue_rub = round(row["revenue"] * fx_rate, 2)
        out.append((
            date_s, "web", "gcc", channel, subchannel, row["traffic_type"],
            "", "",                                            # campaign_id, campaign_name (channel-level, пусто)
            cost_rub, 0, 0, row["sessions"], row["users"], 0,   # cost, clicks, impressions, sessions, users, clients
            row["orders"], revenue_rub, 0,                      # purchases_count, purchases_revenue, customers
            0, 0, 0.0,                                          # new_users, new_customers, new_customers_revenue
        ))
    return out


def _sync_range(frm: date, to: date, conn) -> int:
    token = os.environ["GCC_METRICA_TOKEN"]
    tw_key = os.environ["GCC_TRIPLEWHALE_API_KEY"]
    shop = os.environ["GCC_TW_SHOP_DOMAIN"]

    total = 0
    day = frm
    while day <= to:
        day_s = day.isoformat()
        metrika = fetch_metrika_traffic(METRICA_COUNTER_ID, token, day_s, day_s)
        orders = aggregate_orders_by_channel(fetch_tw_orders(tw_key, shop, day_s, day_s), day_s)
        spend = spend_by_channel(fetch_tw_spend(tw_key, shop, day_s), day_s)
        fx_rate = fx_to_rub("AED", day_s)
        rows = merge_rows(metrika, orders, spend, fx_rate, day_s)

        if conn is None:
            cost_sum = sum(r[8] for r in rows)
            revenue_sum = sum(r[15] for r in rows)
            orders_sum = sum(r[14] for r in rows)
            print(f"lime_gcc: [DRY-RUN] {day_s} → {len(rows)} строк "
                  f"(cost={cost_sum:.2f}₽, revenue={revenue_sum:.2f}₽, orders={orders_sum})")
        else:
            with conn.cursor() as cur:
                cur.execute(DELETE_SQL, (day_s, day_s))
                if rows:
                    psycopg2.extras.execute_values(cur, INSERT_SQL, rows, page_size=500)
            conn.commit()
            print(f"lime_gcc: {day_s} → {len(rows)} строк")

        total += len(rows)
        day += timedelta(days=1)
    return total


def sync_lime_gcc() -> int:
    frm_env = os.environ.get("LIME_GCC_SYNC_FROM")
    to_env = os.environ.get("LIME_GCC_SYNC_TO")
    if frm_env and to_env:
        frm = date.fromisoformat(frm_env)
        to = date.fromisoformat(to_env)
    else:
        to = date.today()
        frm = to - timedelta(days=SYNC_DAYS - 1)

    dry_run = bool(os.environ.get("LIME_GCC_DRY_RUN")) or not os.environ.get("DATABASE_URL")
    if dry_run:
        return _sync_range(frm, to, None)

    url = os.environ["DATABASE_URL"].split("?")[0]
    conn = psycopg2.connect(url, connect_timeout=30)
    try:
        total = _sync_range(frm, to, conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return total


if __name__ == "__main__":
    sync_lime_gcc()
