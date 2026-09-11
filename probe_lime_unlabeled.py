#!/usr/bin/env python3
"""Что PROCONTEXT знает о строках, которые у нас лежат без кампании / в «Others».

Зачем: аудит RU 11.09 нашёл 500+ тыс. сессий за 30 дней в корзинах без campaign_id
(`ya_direct`, `direct`, `{utm_source}`, `yandex.direct/(not set)`, `(not set)`,
`vk-ads-(ex.-mytarget)`, VK.Ads без id). Синк хранит campaign_id только у платных
каналов, поэтому у «Others» мы могли выбросить id сами. Смотрим сырые поля
lc_simple_view: campaign_id, campaign, campaign_name, ad_platform, attribution_type.

Только чтение.
"""
import os
from datetime import datetime, timedelta, timezone

import pymysql

TO = (os.environ.get("PROBE_TO") or "").strip() or (
    datetime.now(timezone.utc) - timedelta(days=2)
).date().isoformat()
FROM = (os.environ.get("PROBE_FROM") or "").strip() or (
    datetime.fromisoformat(TO) - timedelta(days=13)
).date().isoformat()

SOURCES = (
    "ya_direct", "direct", "{utm_source}", "(not set)", "(not+set)",
    "yandex.direct", "vk-ads-(ex.-mytarget)", "vk_ads", "vkads", "ya.go", "yago",
    "google.adwords", "google", "adwords",
)


def show(title, rows, limit=60):
    print(f"\n── {title} ({len(rows)} строк) ──")
    for r in rows[:limit]:
        print("  " + " | ".join(f"{k}={v}" for k, v in r.items()))


def main():
    print(f"[probe] окно {FROM} .. {TO}")
    my = pymysql.connect(
        host=os.environ["LIME_DB_HOST"],
        port=int(os.environ.get("LIME_DB_PORT") or "3306"),
        db=os.environ["LIME_DB_SCHEMA"],
        user=os.environ["LIME_DB_USER"],
        password=os.environ["LIME_DB_PASSWORD"],
        charset="utf8mb4",
        connect_timeout=30,
        cursorclass=pymysql.cursors.DictCursor,
    )
    ph = ",".join(["%s"] * len(SOURCES))
    with my.cursor() as cur:
        # 1. Есть ли campaign_id / сырой campaign у этих источников
        cur.execute(
            f"""
            SELECT data_source, source, medium,
                   SUM(campaign_id NOT IN ('', '(not set)') AND campaign_id IS NOT NULL) with_cid,
                   SUM(campaign NOT IN ('', '(not set)') AND campaign IS NOT NULL) with_raw,
                   COUNT(*) n, SUM(sessions) sessions, SUM(purchases_count) orders
            FROM lc_simple_view
            WHERE date >= %s AND date <= %s AND LOWER(source) IN ({ph})
            GROUP BY 1,2,3 ORDER BY sessions DESC
            """,
            (FROM, TO, *SOURCES),
        )
        show("наличие campaign_id / сырого campaign по источникам", cur.fetchall(), 80)

        # 2. Топ сырых campaign у этих источников (что именно лежит в utm_campaign)
        cur.execute(
            f"""
            SELECT data_source, source, medium, campaign_id, campaign, campaign_name,
                   attribution_type, ad_platform,
                   COUNT(*) n, SUM(sessions) sessions, SUM(purchases_count) orders
            FROM lc_simple_view
            WHERE date >= %s AND date <= %s AND LOWER(source) IN ({ph})
            GROUP BY 1,2,3,4,5,6,7,8 ORDER BY sessions DESC LIMIT 120
            """,
            (FROM, TO, *SOURCES),
        )
        show("топ сырых значений", cur.fetchall(), 120)

        # 3. Всё, что PROCONTEXT считает не платным, но с непустым campaign_id:
        #    синк выбрасывает id у неплатных каналов — сколько теряем
        cur.execute(
            """
            SELECT data_source, source, medium,
                   SUM(campaign_id NOT IN ('', '(not set)') AND campaign_id IS NOT NULL) with_cid,
                   COUNT(*) n, SUM(sessions) sessions, SUM(purchases_count) orders
            FROM lc_simple_view
            WHERE date >= %s AND date <= %s
            GROUP BY 1,2,3 HAVING with_cid > 0 ORDER BY sessions DESC LIMIT 60
            """,
            (FROM, TO),
        )
        show("все source/medium с непустым campaign_id", cur.fetchall(), 60)
    my.close()


if __name__ == "__main__":
    main()
