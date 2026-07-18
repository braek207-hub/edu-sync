# -*- coding: utf-8 -*-
"""sync/lime_kz_cabinet.py — Google Ads KZ → lime_stats (region=kz, subchannel=Google.Adwords).

Google Ads KZ в MySQL (lc_simple_view) НЕТ вообще, поэтому его строки инжектим прямо в lime_stats
(база из MySQL отсутствует). Яндекс Директ KZ (LIME-KZ1) НЕ трогаем — он приходит из MySQL с
пользователями и дообогащается кабинетом (lime_direct_stats) на чтении в хендлере.

Владение срезом: lime.py исключает region='kz' AND subchannel='Google.Adwords' из своего
delete+insert (см. DELETE_SQL там), а этот синк им владеет — не затираем друг друга (как GCC).
Валюта Google KZ = валюта аккаунта (обычно USD) → RUB по ЦБ (fx.to_rub). Пишем cost/clicks/
impressions + имя; заказы/визиты = 0 (у Google KZ нет MySQL-атрибуции).

ENV: DATABASE_URL, LIME_KZ_CABINET_DAYS_BACK (default 30). Запуск: python -m sync.lime_kz_cabinet
"""
import os
from datetime import date, timedelta

import psycopg2
import psycopg2.extras

from sync.fx import CBR_IDS, to_rub as fx_to_rub

DAYS_BACK = int(os.environ.get("LIME_KZ_CABINET_DAYS_BACK") or "30")

SELECT_GOOGLE = """
SELECT date::text AS date, campaign_id, MAX(campaign_name) AS campaign_name,
       COALESCE(currency, 'USD') AS currency,
       SUM(COALESCE(cost, 0))::float AS cost, SUM(COALESCE(clicks, 0))::int AS clicks,
       SUM(COALESCE(impressions, 0))::int AS impressions
FROM lime_google_ads_stats
WHERE region = 'kz' AND date >= %s AND date <= %s
GROUP BY date, campaign_id, currency
"""

DELETE_SQL = """
DELETE FROM lime_stats
WHERE region = 'kz' AND subchannel = 'Google.Adwords' AND date >= %s AND date <= %s
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
    """RUB как есть; валюты из CBR (USD/AED) по курсу ЦБ; иначе не гадаем."""
    c = (currency or "").upper()
    if c in ("RUB", "RUR", ""):
        return float(cost)
    if c in CBR_IDS:
        return float(cost) * fx_to_rub(c, date_s)
    print(f"lime_kz_cabinet: WARN валюта {currency!r} не сконвертирована в рубли")
    return float(cost)


def build_rows(google_rows) -> list[tuple]:
    out: list[tuple] = []
    for r in google_rows:
        cost = _to_rub(r["cost"], r["currency"], r["date"])
        out.append((
            r["date"], "web", "kz", "SEM", "Google.Adwords", "Платный",
            str(r["campaign_id"]) if r["campaign_id"] else "", r["campaign_name"] or "",
            cost, float(r["clicks"]), float(r["impressions"]), 0, 0, 0,
            0, 0.0, 0, 0, 0, 0.0,
        ))
    return out


def sync_lime_kz_cabinet() -> int:
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL не задан")
    to = date.today()
    frm = to - timedelta(days=DAYS_BACK)
    frm_s, to_s = frm.isoformat(), to.isoformat()

    conn = psycopg2.connect(os.environ["DATABASE_URL"].split("?")[0], connect_timeout=30)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(SELECT_GOOGLE, (frm_s, to_s))
            google_rows = cur.fetchall()

        rows = build_rows(google_rows)
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

    print(f"lime_kz_cabinet: {frm_s}..{to_s} → {len(rows)} строк Google KZ")
    return len(rows)


if __name__ == "__main__":
    sync_lime_kz_cabinet()
