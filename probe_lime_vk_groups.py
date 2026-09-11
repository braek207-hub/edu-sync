#!/usr/bin/env python3
"""Что PROCONTEXT кладёт в campaign_id у app-трафика VK Ads (lc_simple_view).

Зачем: с июля 2026 в lime_stats VK-трафик приложения растёт в строке с пустым
campaign_id (10 тыс. сессий/нед), и кампании кабинетов 809330054/814617419,
запущенные после 01.07, не имеют строк витрины вовсе. Гипотеза: в ссылках приходит
id ГРУППЫ VK, а справочник группа→кампания у PROCONTEXT покрывает не все кабинеты.

Только чтение: SELECT в MySQL и в Postgres, ни одной записи.

Окно: PROBE_FROM / PROBE_TO (по умолчанию последние 14 дней, заканчивая позавчера).
"""
import os
from datetime import datetime, timedelta, timezone

import psycopg2
import pymysql

TO = (os.environ.get("PROBE_TO") or "").strip() or (
    datetime.now(timezone.utc) - timedelta(days=2)
).date().isoformat()
FROM = (os.environ.get("PROBE_FROM") or "").strip() or (
    datetime.fromisoformat(TO) - timedelta(days=13)
).date().isoformat()


def mysql_conn():
    return pymysql.connect(
        host=os.environ["LIME_DB_HOST"],
        port=int(os.environ.get("LIME_DB_PORT") or "3306"),
        db=os.environ["LIME_DB_SCHEMA"],
        user=os.environ["LIME_DB_USER"],
        password=os.environ["LIME_DB_PASSWORD"],
        charset="utf8mb4",
        connect_timeout=30,
        cursorclass=pymysql.cursors.DictCursor,
    )


def show(title, rows, limit=40):
    print(f"\n── {title} ({len(rows)} строк) ──")
    for r in rows[:limit]:
        print("  " + " | ".join(f"{k}={v}" for k, v in r.items()))


def main():
    print(f"[probe] окно {FROM} .. {TO}")
    my = mysql_conn()
    with my.cursor() as cur:
        cur.execute("SHOW COLUMNS FROM lc_simple_view")
        cols = [r["Field"] for r in cur.fetchall()]
        print("\nколонки lc_simple_view:", ", ".join(cols))

        # 1. Как выглядит VK-трафик приложения: source/medium/campaign_id/campaign_name
        cur.execute(
            """
            SELECT data_source, source, medium, campaign_id, campaign_name,
                   COUNT(*) n, SUM(sessions) sessions, SUM(purchases_count) orders
            FROM lc_simple_view
            WHERE date >= %s AND date <= %s
              AND (LOWER(source) LIKE '%%vk%%' OR LOWER(source) LIKE '%%mytarget%%')
            GROUP BY 1,2,3,4,5
            ORDER BY sessions DESC
            LIMIT 60
            """,
            (FROM, TO),
        )
        vk = cur.fetchall()
        show("VK-трафик по campaign_id/campaign_name", vk, 60)

        # 2. Все остальные колонки для строк с пустым campaign_id — вдруг id группы лежит
        #    в другом поле (utm_content / ad_group / content …)
        extra = [c for c in cols if c not in (
            "date", "data_source", "region", "source", "medium", "campaign_id",
            "campaign_name", "cost", "clicks", "impressions", "sessions", "users",
            "clients", "purchases_count", "purchases_revenue", "customers",
            "new_users", "new_customers", "new_customers_revenue",
        )]
        print("\nдоп. колонки помимо известных синку:", extra or "нет")
        if extra:
            sel = ", ".join(f"`{c}`" for c in extra)
            cur.execute(
                f"""
                SELECT {sel}, COUNT(*) n, SUM(sessions) sessions
                FROM lc_simple_view
                WHERE date >= %s AND date <= %s AND data_source = 'app'
                  AND (LOWER(source) LIKE '%%vk%%' OR LOWER(source) LIKE '%%mytarget%%')
                  AND (campaign_id IS NULL OR campaign_id IN ('', '(not set)'))
                GROUP BY {sel}
                ORDER BY sessions DESC
                LIMIT 40
                """,
                (FROM, TO),
            )
            show("доп. колонки у app-строк VK без campaign_id", cur.fetchall(), 40)

        # 3. Сырой `campaign` у app-строк VK без campaign_id: это id группы VK?
        cur.execute(
            """
            SELECT source, medium, ad_platform, campaign,
                   COUNT(*) n, SUM(sessions) sessions, SUM(purchases_count) orders,
                   SUM(purchases_revenue) revenue
            FROM lc_simple_view
            WHERE date >= %s AND date <= %s AND data_source = 'app'
              AND (LOWER(source) LIKE '%%vk%%' OR LOWER(source) LIKE '%%mytarget%%')
              AND (campaign_id IS NULL OR campaign_id IN ('', '(not set)'))
            GROUP BY 1,2,3,4
            ORDER BY sessions DESC
            LIMIT 80
            """,
            (FROM, TO),
        )
        raw = cur.fetchall()
        show("сырой campaign у app-строк VK без campaign_id", raw, 80)

    pg = psycopg2.connect(os.environ["DATABASE_URL"].split("?")[0], connect_timeout=30)
    gids = sorted({str(r["campaign"]) for r in raw if str(r["campaign"] or "").isdigit()})
    if gids:
        with pg.cursor() as cur:
            cur.execute(
                "SELECT entity_id, kind, ad_plan_id, cabinet, name FROM lime_vk_entities WHERE entity_id = ANY(%s)",
                (gids,),
            )
            found = {r[0]: r[1:] for r in cur.fetchall()}
        tot = sum(int(r["sessions"] or 0) for r in raw)
        ok = sum(int(r["sessions"] or 0) for r in raw if str(r["campaign"]) in found)
        print(f"\n── сырой campaign против lime_vk_entities: {len(gids)} id, найдено {len(found)}; "
              f"сессий резолвится {ok} из {tot} ──")
        for i in gids:
            print(f"  {i}: {found.get(i, 'нет в справочнике')}")

    # 4. Сверка: campaign_id из MySQL → это ad_plan или ad_group по нашему справочнику
    ids = sorted({str(r["campaign_id"]) for r in vk if r["campaign_id"] not in (None, "", "(not set)")})
    if ids:
        with pg.cursor() as cur:
            cur.execute(
                "SELECT entity_id, kind FROM lime_vk_entities WHERE entity_id = ANY(%s)",
                (ids,),
            )
            kinds = {}
            for eid, kind in cur.fetchall():
                kinds[kind] = kinds.get(kind, 0) + 1
        print(f"\n── campaign_id MySQL против lime_vk_entities: {len(ids)} id, по видам {kinds} ──")
    pg.close()
    my.close()


if __name__ == "__main__":
    main()
