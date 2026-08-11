# -*- coding: utf-8 -*-
"""Новые/повторные покупатели в кампанийной грани разделов.

«Новый» = первая покупка клиента с начала наших логов (2026-05-25, старее не
видим — оговорка в глоссарии дашборда). Стейт lime_buyer_state вечный и узкий:
(платформа, клиент, день первой покупки).

Идемпотентность и произвольный порядок бэкфилла: стейт обновляется правилом
LEAST (более ранняя найденная покупка сдвигает первую назад), «новый в день D» =
first_buy_day >= D после апсерта. Повторная заливка дня даёт те же числа.
"""
from collections import defaultdict

import psycopg2.extras

from sync.db import get_connection


def classify_new(day: str, stored: dict, buyers: set) -> set:
    """Кто из покупателей дня — новый. stored: cid -> first_buy_day (ISO, уже
    с учётом покупки дня, т.е. ПОСЛЕ апсерта стейта)."""
    return {cid for cid in buyers if stored.get(cid, day) >= day}


def apply_new_measures(day: str, platform: str, orders: list, campaign_rows: list) -> list:
    """Дополняет строки кампанийной грани мерами new_buyers/new_revenue.

    orders — список заказов дня: (client_id, channel, campaigns_tuple,
    {bought_section: [items, revenue]}); campaigns_tuple содержит '' для
    остатка канала. campaign_rows — кортежи под TABLES['campaign'] БЕЗ
    new-колонок; возвращает кортежи С ними (порядок колонок в lime_sections_db).
    """
    buyers = {str(o[0]) for o in orders}
    stored: dict = {}
    if buyers:
        with get_connection() as conn:
            with conn.cursor() as cur:
                ids = sorted(buyers)
                psycopg2.extras.execute_batch(
                    cur,
                    """insert into lime_buyer_state (platform, client_id, first_buy_day)
                       values (%s, %s, %s)
                       on conflict (platform, client_id)
                       do update set first_buy_day =
                         least(lime_buyer_state.first_buy_day, excluded.first_buy_day)""",
                    [(platform, cid, day) for cid in ids], page_size=500,
                )
                cur.execute(
                    "select client_id, first_buy_day from lime_buyer_state "
                    "where platform = %s and client_id = any(%s)",
                    (platform, ids),
                )
                stored = {cid: d.isoformat() for cid, d in cur.fetchall()}
            conn.commit()
    new_cids = classify_new(day, stored, buyers)

    agg = defaultdict(lambda: [set(), 0.0])   # (ch, camp, sec) -> new buyers, new revenue
    for cid, ch, camps, by_sec in orders:
        if str(cid) not in new_cids:
            continue
        for camp in camps:
            for sec, (_items, rev) in by_sec.items():
                cell = agg[(ch, camp, sec)]
                cell[0].add(str(cid))
                cell[1] += rev

    out = []
    for row in campaign_rows:
        ch, camp, sec = row[2], row[3], row[4]
        nb, nrev = agg.get((ch, camp, sec), (set(), 0.0))
        out.append(row + (len(nb), round(nrev, 2)))
    return out
