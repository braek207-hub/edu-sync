#!/usr/bin/env python3
"""Sync lc_simple_view from LIME MySQL → Supabase (aggregated by channel)."""
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import pymysql
import psycopg2
import psycopg2.extras

SYNC_DAYS = int(os.environ.get("LIME_SYNC_DAYS", "7"))
DATE_FROM = os.environ.get("LIME_DATE_FROM")  # override for historical backfill
DATE_TO   = os.environ.get("LIME_DATE_TO")    # override for historical backfill

MYSQL_CFG = dict(
    host=os.environ["LIME_DB_HOST"],
    port=int(os.environ.get("LIME_DB_PORT", "3306")),
    database=os.environ["LIME_DB_SCHEMA"],
    user=os.environ["LIME_DB_USER"],
    password=os.environ["LIME_DB_PASSWORD"],
    charset="utf8mb4",
    connect_timeout=30,
    cursorclass=pymysql.cursors.DictCursor,
)

PG_URL = os.environ["DATABASE_URL"].split("?")[0]

PAID_CHANNELS = {"SEM", "SMM paid", "Retargeting"}


def classify(source: str, medium: str):
    s = (source or "").lower().strip()
    m = (medium or "").lower().strip()

    # SEM
    if any(x in s for x in ["ya.direct", "yandex.direct", "y a.direct"]):
        return "SEM", "Яндекс.Директ"
    if "yandex" in s and "market" not in s and m == "cpc":
        return "SEM", "Яндекс.Директ"
    if "google" in s and "brand" not in s and m == "cpc":
        return "SEM", "Google.Adwords"
    if "google" in s and "brand" in s and m == "cpc":
        return "SEM", "Google.Adwords Brand"
    if "turbotarget" in s:
        return "SEM", "Яндекс.Директ"
    if m in ("cpm",) and s not in ("", "(not set)"):
        return "SEM", s.capitalize()

    # SMM paid
    if any(x in s for x in ["vk_ads", "vkads"]):
        return "SMM paid", "VK.Ads"
    if "vkontakte" in s and m in ("cpc", "cpa"):
        return "SMM paid", "VK.Ads"
    if "mytarget" in s and m in ("cpc", "cpa"):
        return "SMM paid", "MyTarget"
    if "Yandex.Direct" in source:
        return "SEM", "Яндекс.Директ"

    # Retargeting
    if "soloway" in s:
        return "Retargeting", "Soloway"
    if "rtbhouse" in s or m == "retargeting":
        return "Retargeting", "RTB-House"

    # CRM
    if any(x in s for x in ["manual_mindbox", "mindbox"]):
        return "CRM", "Mindbox" if "mindbox" in s else "Авторассылка"
    if s == "sms" or m == "sms":
        return "CRM", "SMS"
    if s == "email" or m == "email":
        return "CRM", "Email"
    if s == "push" or m == "push":
        return "CRM", "Push"

    # SEO
    if "yandex" in s and m == "organic":
        return "SEO", "SEO Yandex"
    if s == "yandex search":
        return "SEO", "SEO Yandex"
    if "google" in s and m == "organic":
        return "SEO", "SEO Google"
    if m == "organic":
        return "SEO", "SEO Others"
    if any(x in s for x in ["yandex.market", "yandex_market"]):
        return "SEO", "Яндекс.Маркет"

    # Referrals
    if m == "referral":
        return "Referrals", "Реферал"
    if any(x in s for x in ["wildberries", "ozon", "sbermarket", "sbermegamarket"]):
        return "Referrals", s.capitalize()

    # SMM organic
    if any(x in s for x in ["vkontakte", "instagram", "telegram", "facebook",
                              "youtube", "dzen", "tg", "vk"]) and m not in ("cpc", "cpa"):
        return "SMM (organic)", s.capitalize()
    if m in ("social", "messenger"):
        return "SMM (organic)", "Others"

    # OLV / Others
    if "olv" in s or m in ("smart-tv", "olv", "video_network"):
        return "Others", "OLV"

    # Direct
    if s in ("(direct)", "(not set)", "", "(no data)", "(undefined)") or \
       m in ("(none)", "(not set)", ""):
        return "Direct", "Direct"

    return "Others", s or m or "Unknown"


def aggregate(rows):
    agg = defaultdict(lambda: {
        "cost": 0.0, "clicks": 0.0, "impressions": 0.0,
        "sessions": 0, "purchases_count": 0, "purchases_revenue": 0.0,
        "customers": 0, "new_users": 0, "new_customers": 0,
    })

    for r in rows:
        channel, subchannel = classify(r["source"] or "", r["medium"] or "")
        is_paid = channel in PAID_CHANNELS
        cid = r["campaign_id"]
        cname = r["campaign_name"]
        bad = {None, "(not set)", "<NA>", ""}

        key = (
            str(r["date"]),
            r["data_source"] or "",
            r["region"] or "",
            channel,
            subchannel,
            "Платный" if is_paid else "Бесплатный",
            str(cid) if is_paid and cid not in bad else "",
            str(cname) if is_paid and cname not in bad else "",
        )
        a = agg[key]
        a["cost"]             += float(r["cost"] or 0)
        a["clicks"]           += float(r["clicks"] or 0)
        a["impressions"]      += float(r["impressions"] or 0)
        a["sessions"]         += int(r["sessions"] or 0)
        a["purchases_count"]  += int(r["purchases_count"] or 0)
        a["purchases_revenue"]+= float(r["purchases_revenue"] or 0)
        a["customers"]        += int(r["customers"] or 0)
        a["new_users"]        += int(r["new_users"] or 0)
        a["new_customers"]    += int(r["new_customers"] or 0)

    return agg


def main():
    if DATE_FROM and DATE_TO:
        where = f'date BETWEEN "{DATE_FROM}" AND "{DATE_TO}"'
        label = f"{DATE_FROM} → {DATE_TO}"
        cutoff_from = DATE_FROM
        cutoff_to   = DATE_TO
    else:
        where = f"date >= CURDATE() - INTERVAL {SYNC_DAYS} DAY"
        label = f"last {SYNC_DAYS} days"
        cutoff_from = (datetime.now(timezone.utc) - timedelta(days=SYNC_DAYS)).date()
        cutoff_to   = datetime.now(timezone.utc).date()

    print(f"[lime-sync] fetching {label}...")

    conn_my = pymysql.connect(**MYSQL_CFG)
    try:
        with conn_my.cursor() as cur:
            cur.execute(f"SELECT * FROM lc_simple_view WHERE {where}")
            rows = cur.fetchall()
    finally:
        conn_my.close()

    print(f"[lime-sync] raw rows: {len(rows)}")
    if not rows:
        print("[lime-sync] nothing to sync")
        return

    agg = aggregate(rows)
    print(f"[lime-sync] aggregated rows: {len(agg)}")

    conn_pg = psycopg2.connect(PG_URL)
    try:
        with conn_pg.cursor() as cur:
            cur.execute(
                "DELETE FROM lime_stats WHERE date >= %s AND date <= %s",
                (cutoff_from, cutoff_to)
            )
            deleted = cur.rowcount

            insert_sql = """
            INSERT INTO lime_stats (
                date, data_source, region, channel, subchannel, traffic_type,
                campaign_id, campaign_name,
                cost, clicks, impressions, sessions,
                purchases_count, purchases_revenue, customers, new_users, new_customers
            ) VALUES %s
            """

            data = [
                (
                    key[0], key[1], key[2], key[3], key[4], key[5], key[6], key[7],
                    v["cost"], v["clicks"], v["impressions"], v["sessions"],
                    v["purchases_count"], v["purchases_revenue"],
                    v["customers"], v["new_users"], v["new_customers"],
                )
                for key, v in agg.items()
            ]

            psycopg2.extras.execute_values(cur, insert_sql, data, page_size=500)
            conn_pg.commit()

        print(f"[lime-sync] deleted {deleted}, inserted {len(data)} rows. Done.")
    except Exception:
        conn_pg.rollback()
        raise
    finally:
        conn_pg.close()


if __name__ == "__main__":
    main()
