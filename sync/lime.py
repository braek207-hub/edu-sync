"""Sync lc_simple_view from LIME MySQL → Supabase, classified & aggregated by channel.

Used for both the daily cron (last N days) and one-off historical backfills
(explicit LIME_SYNC_FROM/LIME_SYNC_TO range). Raw granular rows are never
stored — only classified + aggregated rows go into lime_stats.
"""
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
import pymysql

SYNC_DAYS = int(os.environ.get("LIME_SYNC_DAYS") or "7")
SYNC_FROM = (os.environ.get("LIME_SYNC_FROM") or "").strip() or None
SYNC_TO = (os.environ.get("LIME_SYNC_TO") or "").strip() or None


def _mysql_cfg() -> dict:
    return dict(
        host=os.environ["LIME_DB_HOST"],
        port=int(os.environ.get("LIME_DB_PORT") or "3306"),
        db=os.environ["LIME_DB_SCHEMA"],
        user=os.environ["LIME_DB_USER"],
        password=os.environ["LIME_DB_PASSWORD"],
        charset="utf8mb4",
        connect_timeout=30,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _pg_url() -> str:
    return os.environ["DATABASE_URL"].split("?")[0]


PAID_CHANNELS = {"SEM", "SMM paid", "Retargeting"}


def classify(source: str, medium: str):
    """Map raw (source, medium) → (channel, subchannel) per LIME's marketing taxonomy."""
    s = (source or "").lower().strip()
    m = (medium or "").lower().strip()

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

    # `vk-ads-(ex.-mytarget)` / medium (not set) — сессии приложения без UTM, которые
    # AppMetrica подписала источником УСТАНОВКИ (publisher «VK Ads (ex. myTarget)»).
    # Это платные установки, не органика — тот же случай, что `yandex.direct / (not set)`
    # в SEM выше. До 11.09.2026 падало в SMM (organic): ~140 тыс. сессий/мес.
    if any(x in s for x in ["vk_ads", "vkads", "vk-ads"]):
        return "SMM paid", "VK.Ads"
    if "vkontakte" in s and m in ("cpc", "cpa"):
        return "SMM paid", "VK.Ads"
    if "mytarget" in s and m in ("cpc", "cpa"):
        return "SMM paid", "MyTarget"
    if "Yandex.Direct" in source:
        return "SEM", "Яндекс.Директ"

    if "soloway" in s:
        return "Retargeting", "Soloway"
    if "rtbhouse" in s or m == "retargeting":
        return "Retargeting", "RTB-House"

    if any(x in s for x in ["manual_mindbox", "mindbox"]):
        return "CRM", "Mindbox" if "mindbox" in s else "Авторассылка"
    if s == "sms" or m == "sms":
        return "CRM", "SMS"
    if s == "email" or m == "email":
        return "CRM", "Email"
    if s == "push" or m == "push":
        return "CRM", "Push"

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

    if m == "referral":
        return "Referrals", "Реферал"
    if any(x in s for x in ["wildberries", "ozon", "sbermarket", "sbermegamarket"]):
        return "Referrals", s.capitalize()

    if any(x in s for x in ["vkontakte", "instagram", "telegram", "facebook",
                            "youtube", "dzen", "tg", "vk"]) and m not in ("cpc", "cpa"):
        return "SMM (organic)", s.capitalize()
    if m in ("social", "messenger"):
        return "SMM (organic)", "Others"

    if "olv" in s or m in ("smart-tv", "olv", "video_network"):
        return "Others", "OLV"

    if s in ("(direct)", "(not set)", "", "(no data)", "(undefined)") or \
            m in ("(none)", "(not set)", ""):
        return "Direct", "Direct"

    return "Others", s or m or "Unknown"


def load_vk_group_map(pg_url: str) -> dict:
    """Группа VK → (кампания ad_plan, её имя из кабинета).

    В ссылках VK Ads макрос {{campaign_id}} несёт id ГРУППЫ (наследие myTarget), PROCONTEXT
    кладёт его в сырой `campaign`, а campaign_id/campaign_name ставит по своему
    справочнику. Тот справочник знает не все кабинеты: группы кабинетов 809330054/814617419/
    814620282, созданные после 01.07.2026, остались без campaign_id (замер 11.09.2026:
    20 тыс. сессий за две недели ушли в строку без кампании, все 14 групп есть в
    lime_vk_entities). Свой справочник собирает sync/lime_vk_ads.py по всем 4 кабинетам.
    """
    conn = psycopg2.connect(pg_url, connect_timeout=30)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.entity_id, e.ad_plan_id, s.campaign_name
                FROM lime_vk_entities e
                LEFT JOIN LATERAL (
                    SELECT campaign_name FROM lime_vk_ads_stats
                    WHERE campaign_id = e.ad_plan_id AND campaign_name IS NOT NULL
                    ORDER BY date DESC LIMIT 1
                ) s ON TRUE
                WHERE e.kind = 'ad_group' AND e.ad_plan_id IS NOT NULL
                """
            )
            return {str(g): (str(p), n or "") for g, p, n in cur.fetchall()}
    finally:
        conn.close()


_BAD_ID = {None, "(not set)", "<NA>", ""}


def resolve_vk_campaign(r, vk_groups):
    """(campaign_id, campaign_name) строки: значение PROCONTEXT, если оно есть, иначе —
    группа VK из сырого `campaign` через наш справочник. Только для источника VK.Ads:
    числовой `campaign` у других источников — не id группы."""
    cid, cname = r["campaign_id"], r["campaign_name"]
    if cid not in _BAD_ID or not vk_groups:
        return cid, cname
    s = (r.get("source") or "").lower()
    if not any(x in s for x in ["vk_ads", "vkads"]):
        return cid, cname
    hit = vk_groups.get(str(r.get("campaign") or "").strip())
    return hit if hit else (cid, cname)


def aggregate(rows, vk_groups=None):
    agg = defaultdict(lambda: {
        "cost": 0.0, "clicks": 0.0, "impressions": 0.0,
        "sessions": 0, "users": 0, "clients": 0,
        "purchases_count": 0, "purchases_revenue": 0.0,
        "customers": 0, "new_users": 0, "new_customers": 0,
        "new_customers_revenue": 0.0,
    })

    for r in rows:
        channel, subchannel = classify(r["source"] or "", r["medium"] or "")
        is_paid = channel in PAID_CHANNELS
        cid, cname = resolve_vk_campaign(r, vk_groups)
        bad = _BAD_ID

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
        a["cost"] += float(r["cost"] or 0)
        a["clicks"] += float(r["clicks"] or 0)
        a["impressions"] += float(r["impressions"] or 0)
        a["sessions"] += int(r["sessions"] or 0)
        a["users"] += int(r["users"] or 0)
        a["clients"] += int(r["clients"] or 0)
        a["purchases_count"] += int(r["purchases_count"] or 0)
        a["purchases_revenue"] += float(r["purchases_revenue"] or 0)
        a["customers"] += int(r["customers"] or 0)
        a["new_users"] += int(r["new_users"] or 0)
        a["new_customers"] += int(r["new_customers"] or 0)
        a["new_customers_revenue"] += float(r["new_customers_revenue"] or 0)

    return agg


INSERT_SQL = """
INSERT INTO lime_stats (
    date, data_source, region, channel, subchannel, traffic_type,
    campaign_id, campaign_name,
    cost, clicks, impressions, sessions, users, clients,
    purchases_count, purchases_revenue, customers,
    new_users, new_customers, new_customers_revenue
) VALUES %s
"""

# Регионы lime_stats, которыми владеют ОТДЕЛЬНЫЕ синки: каждый сам удаляет и пишет свой
# срез, а этот синк-витрина обязан их не трогать.
#   gcc        → sync/lime_gcc.py (Залив: Метрика + Triple Whale)
#   kz_metrika → sync/lime_kz_metrika.py (KZ напрямую из Метрики)
# Держим СПИСКОМ, а не условием в тексте SQL: добавление четвёртого синка обязано
# зарегистрировать регион здесь. Пропуск такой регистрации — реальный баг (2026-07-18):
# kz_metrika отсутствовал в исключениях, и ежедневный прогон этого синка стирал бы
# чужой срез целиком, молча и каждый день.
FOREIGN_REGIONS = ("gcc", "kz_metrika", "kz_roistat")

_FOREIGN_LIST = ", ".join(f"'{r}'" for r in FOREIGN_REGIONS)
DELETE_SQL = f"""
DELETE FROM lime_stats
WHERE date >= %s AND date <= %s AND (region IS NULL OR region NOT IN ({_FOREIGN_LIST}))
"""


def agg_to_rows(agg):
    return [
        (
            key[0], key[1], key[2], key[3], key[4], key[5], key[6], key[7],
            v["cost"], v["clicks"], v["impressions"], v["sessions"],
            v["users"], v["clients"],
            v["purchases_count"], v["purchases_revenue"],
            v["customers"], v["new_users"], v["new_customers"],
            v["new_customers_revenue"],
        )
        for key, v in agg.items()
    ]


def date_chunks(date_from: str, date_to: str):
    d = datetime.strptime(date_from, "%Y-%m-%d").date()
    end = datetime.strptime(date_to, "%Y-%m-%d").date()
    while d <= end:
        yield d.isoformat()
        d += timedelta(days=1)


def sync_chunk(conn_my, day_from: str, day_to: str, vk_groups=None):
    with conn_my.cursor() as cur:
        cur.execute(
            "SELECT * FROM lc_simple_view WHERE date >= %s AND date <= %s",
            (day_from, day_to),
        )
        rows = cur.fetchall()

    if not rows:
        return 0, 0

    agg = aggregate(rows, vk_groups)
    data = agg_to_rows(agg)

    conn_pg = psycopg2.connect(_pg_url(), connect_timeout=30)
    try:
        with conn_pg.cursor() as cur:
            cur.execute(DELETE_SQL, (day_from, day_to))
            psycopg2.extras.execute_values(cur, INSERT_SQL, data, page_size=500)
        conn_pg.commit()
    except Exception:
        conn_pg.rollback()
        raise
    finally:
        conn_pg.close()

    return len(rows), len(data)


def sync_lime() -> None:
    range_mode = bool(SYNC_FROM and SYNC_TO)

    if range_mode:
        date_from, date_to = SYNC_FROM, SYNC_TO
        label = f"{SYNC_FROM} -> {SYNC_TO} (chunked by day)"
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=SYNC_DAYS)
        date_from = cutoff.date().isoformat()
        date_to = datetime.now(timezone.utc).date().isoformat()
        label = f"last {SYNC_DAYS} days"

    print(f"[lime-sync] syncing {label}...")

    # Справочник грузится ОДИН раз и обязан быть непустым: день переписывается целиком
    # (DELETE+INSERT), и пустая карта при зелёном прогоне тихо вернула бы VK-кампании
    # в строку без campaign_id — та же ловушка, что была у синка AppMetrica.
    vk_groups = load_vk_group_map(_pg_url())
    if not vk_groups:
        raise RuntimeError("lime_vk_entities пуст: справочник групп VK недоступен, синк остановлен")
    print(f"[lime-sync] справочник групп VK: {len(vk_groups)}")

    conn_my = pymysql.connect(**_mysql_cfg())
    try:
        total_raw = 0
        total_agg = 0
        for day in date_chunks(date_from, date_to):
            conn_my.ping(reconnect=True)
            raw, agg_n = sync_chunk(conn_my, day, day, vk_groups)
            total_raw += raw
            total_agg += agg_n
            print(f"[lime-sync] {day}: raw={raw} -> aggregated={agg_n}")

        print(f"[lime-sync] done. total raw={total_raw}, total aggregated={total_agg}")
    finally:
        conn_my.close()


if __name__ == "__main__":
    sync_lime()
