# -*- coding: utf-8 -*-
"""sync/lime_kz_cabinet.py — KZ рекламный расход из кабинетов → lime_stats (region='kz', channel='SEM').

Кабинетные данные (Яндекс Директ LIME-KZ1 + Google Ads KZ) чище MySQL-среза, поэтому этим срезом
lime_stats владеет ОТДЕЛЬНЫЙ ингест (по образцу GCC): lime.py исключает region='kz'&channel='SEM'
из своего delete+insert, а этот синк его пишет. Так два ингеста не затирают друг друга.

Валюта: Яндекс KZ = рубли (как есть), Google KZ = USD → RUB по курсу ЦБ (sync/fx.py).
Пишет ТОЛЬКО рекламные метрики (cost/clicks/impressions). Заказы/выручка/визиты KZ остаются из
MySQL (lime.py, region='kz', прочие каналы). Гранулярность — per (date, campaign).

ENV: DATABASE_URL, LIME_KZ_CABINET_DAYS_BACK (default 30).
Запуск: python -m sync.lime_kz_cabinet
"""
import os
from datetime import date, timedelta

import psycopg2
import psycopg2.extras

from sync.fx import CBR_IDS, to_rub as fx_to_rub

DAYS_BACK = int(os.environ.get("LIME_KZ_CABINET_DAYS_BACK") or "30")

# Кабинет Яндекс Директ KZ (LIME-KZ1) — расход уже в рублях.
SELECT_YANDEX = """
SELECT date::text AS date, campaign_id, MAX(campaign_name) AS campaign_name,
       SUM(COALESCE(cost, 0))::float AS cost,
       SUM(COALESCE(clicks, 0))::int AS clicks,
       SUM(COALESCE(impressions, 0))::int AS impressions
FROM lime_direct_stats
WHERE client_login = 'LIME-KZ1' AND date >= %s AND date <= %s
GROUP BY date, campaign_id
"""

# Кабинет Google Ads KZ — расход в валюте аккаунта (обычно USD), конвертируем на чтении.
SELECT_GOOGLE = """
SELECT date::text AS date, campaign_id, MAX(campaign_name) AS campaign_name,
       COALESCE(currency, 'USD') AS currency,
       SUM(COALESCE(cost, 0))::float AS cost,
       SUM(COALESCE(clicks, 0))::int AS clicks,
       SUM(COALESCE(impressions, 0))::int AS impressions
FROM lime_google_ads_stats
WHERE region = 'kz' AND date >= %s AND date <= %s
GROUP BY date, campaign_id, currency
"""

DELETE_SQL = """
DELETE FROM lime_stats
WHERE region = 'kz' AND channel = 'SEM' AND date >= %s AND date <= %s
"""

INSERT_SQL = """
INSERT INTO lime_stats (
    date, data_source, region, channel, subchannel, traffic_type,
    campaign_id, campaign_name,
    cost, clicks, impressions, sessions, users, clients,
    purchases_count, purchases_revenue, customers,
    new_users, new_customers, new_customers_revenue
) VALUES %s
"""


def _to_rub(cost, currency, date_s) -> float:
    """Расход кабинета → рубли. RUB как есть, USD по курсу ЦБ, иначе не гадаем курс."""
    c = (currency or "").upper()
    if c in ("RUB", "RUR", ""):
        return float(cost)
    if c in CBR_IDS:
        return float(cost) * fx_to_rub(c, date_s)
    print(f"lime_kz_cabinet: WARN неизвестная валюта {currency!r} — cost НЕ сконвертирован в рубли")
    return float(cost)


def _row(date_s, subchannel, campaign_id, campaign_name, cost, clicks, impressions):
    # Только рекламные метрики; заказы/визиты KZ — из MySQL (lime.py). data_source='web'
    # (детализацию web/app по кампаниям опустим на этом слое).
    return (
        date_s, "web", "kz", "SEM", subchannel, "Платный",
        str(campaign_id) if campaign_id else "", campaign_name or "",
        float(cost), float(clicks), float(impressions), 0, 0, 0,
        0, 0.0, 0, 0, 0, 0.0,
    )


def build_rows(yandex_rows, google_rows) -> list[tuple]:
    out: list[tuple] = []
    for r in yandex_rows:
        out.append(_row(r["date"], "Яндекс.Директ", r["campaign_id"], r["campaign_name"],
                        r["cost"], r["clicks"], r["impressions"]))
    for r in google_rows:
        cost_rub = _to_rub(r["cost"], r["currency"], r["date"])
        out.append(_row(r["date"], "Google.Adwords", r["campaign_id"], r["campaign_name"],
                        cost_rub, r["clicks"], r["impressions"]))
    return out


def sync_lime_kz_cabinet() -> int:
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL не задан")
    to = date.today()
    frm = to - timedelta(days=DAYS_BACK)
    frm_s, to_s = frm.isoformat(), to.isoformat()

    url = os.environ["DATABASE_URL"].split("?")[0]
    conn = psycopg2.connect(url, connect_timeout=30)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(SELECT_YANDEX, (frm_s, to_s))
            yandex_rows = cur.fetchall()
            cur.execute(SELECT_GOOGLE, (frm_s, to_s))
            google_rows = cur.fetchall()

        rows = build_rows(yandex_rows, google_rows)
        with conn.cursor() as cur:
            cur.execute(DELETE_SQL, (frm_s, to_s))
            if rows:
                psycopg2.extras.execute_values(cur, INSERT_SQL, rows, page_size=500)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"lime_kz_cabinet: {frm_s}..{to_s} → {len(rows)} строк "
          f"(Яндекс {len(yandex_rows)}, Google {len(google_rows)})")
    return len(rows)


if __name__ == "__main__":
    sync_lime_kz_cabinet()
