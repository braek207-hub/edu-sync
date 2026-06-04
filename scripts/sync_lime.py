#!/usr/bin/env python3
"""Sync lc_simple_view from LIME MySQL → Supabase PostgreSQL (last N days)."""
import os
import json
from datetime import datetime, timedelta, timezone
import pymysql
import psycopg2
import psycopg2.extras

SYNC_DAYS = int(os.environ.get("LIME_SYNC_DAYS", "7"))

MYSQL_CFG = dict(
    host=os.environ["LIME_DB_HOST"],
    port=int(os.environ.get("LIME_DB_PORT", "3306")),
    db=os.environ["LIME_DB_SCHEMA"],
    user=os.environ["LIME_DB_USER"],
    password=os.environ["LIME_DB_PASSWORD"],
    charset="utf8mb4",
    connect_timeout=15,
    cursorclass=pymysql.cursors.DictCursor,
)

PG_URL = os.environ["DATABASE_URL"].split("?")[0]


def main():
    print(f"[lime-sync] syncing last {SYNC_DAYS} days...")

    # 1. Read from MySQL
    conn_my = pymysql.connect(**MYSQL_CFG)
    try:
        with conn_my.cursor() as cur:
            cur.execute(
                "SELECT * FROM lc_simple_view WHERE date >= CURDATE() - INTERVAL %s DAY",
                (SYNC_DAYS,),
            )
            rows = cur.fetchall()
    finally:
        conn_my.close()

    print(f"[lime-sync] fetched {len(rows)} rows from MySQL")

    if not rows:
        print("[lime-sync] nothing to sync")
        return

    # 2. Write to Supabase
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SYNC_DAYS)).date()

    conn_pg = psycopg2.connect(PG_URL)
    try:
        with conn_pg.cursor() as cur:
            cur.execute("DELETE FROM lime_stats WHERE date >= %s", (cutoff,))
            deleted = cur.rowcount

            insert_sql = """
            INSERT INTO lime_stats (
                date, data_source, device, operating_system, attribution_type,
                source_type, source, medium, campaign, ad_platform,
                ad_platform_account, campaign_id, campaign_name,
                region, region_continent, region_country, region_district,
                region_area, region_city,
                impressions, clicks, cost, sessions, clients, users,
                purchases_count, purchases_revenue, customers, new_users,
                new_customers, new_customers_revenue, customers_cost
            ) VALUES %s
            """

            data = [
                (
                    r["date"],
                    r["data_source"] or "",
                    r["device"],
                    r["operating_system"],
                    r["attribution_type"],
                    r["source_type"],
                    r["source"],
                    r["medium"],
                    r["campaign"],
                    r["ad_platform"],
                    r["ad_platform_account"],
                    r["campaign_id"],
                    r["campaign_name"],
                    r["region"],
                    r["regionContinent"],
                    r["regionCountry"],
                    r["regionDistrict"],
                    r["regionArea"],
                    r["regionCity"],
                    r["impressions"],
                    r["clicks"],
                    r["cost"],
                    r["sessions"],
                    r["clients"],
                    r["users"],
                    r["purchases_count"],
                    r["purchases_revenue"],
                    r["customers"],
                    r["new_users"],
                    r["new_customers"],
                    r["new_customers_revenue"],
                    r["customers_cost"],
                )
                for r in rows
            ]

            psycopg2.extras.execute_values(cur, insert_sql, data, page_size=500)
            conn_pg.commit()

        print(f"[lime-sync] deleted {deleted} old rows, inserted {len(rows)} rows. Done.")
    except Exception:
        conn_pg.rollback()
        raise
    finally:
        conn_pg.close()


if __name__ == "__main__":
    main()
