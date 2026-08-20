# -*- coding: utf-8 -*-
"""sync/lime_gcc_counters.py — сверочная витрина счётчиков GCC → lime_gcc_counter_daily.

Второй источник для gcc-строк дашборда (решение Павла 2026-08-20: «Метрика общая по
регионам»): рядом с главными числами (GA4-трафик + TW-заказы в lime_stats) кладём
независимый взгляд счётчиков:
- source='metrika' — Яндекс.Метрика 98232701: визиты/пользователи по дате×стране×каналу×
  кампании (gcc_metrika.fetch_metrika_traffic, канон каналов map_metrika_channel).
  ecommerce на счётчике НЕ настроен (зонд 2026-08-20: 27k визитов, 0 покупок) —
  заказы/выручка Метрики всегда 0 и в дашборд не выводятся.
- source='ga4' — GA4 417919368: ecommerce-заказы/выручка по дате×стране×кампании
  (ecommercePurchases/purchaseRevenue). Выручка в валюте property; сверено с TW:
  W32 = 287 заказов/3.56M против TW linear 349/4.64M ₽ — масштаб рублёвый, конвертации нет.
  GA4-трафик сюда НЕ пишем: он и есть главный трафик gcc-строк (сверять сам с собой нечего).

Дашборд обогащает gcc-строки по (date, country, campaign_id) с фолбэком
(date, country, channel, subchannel) — паттерн RU (enrich-lime-metrika-ru).

ENV: DATABASE_URL, GCC_METRICA_TOKEN (+GCC_METRICA_COUNTER_ID), GOOGLE_APPLICATION_CREDENTIALS/
LIME_REPORTS_SA_JSON (+GCC_GA4_PROPERTY), LIME_GCC_COUNTERS_FROM/TO или LIME_GCC_COUNTERS_DAYS
(default 7), LIME_GCC_COUNTERS_DRY_RUN.
Запуск: python -m sync.lime_gcc_counters
"""
import os
from datetime import date, timedelta

import psycopg2
import psycopg2.extras

from sync.db import _database_url
from sync.gcc_channels import map_ga4_channel
from sync.gcc_ga4 import GA4_PROPERTY, run_report
from sync.gcc_metrika import fetch_metrika_traffic
from sync.metrika_channels import map_metrika_channel

COUNTER_ID = int(os.environ.get("GCC_METRICA_COUNTER_ID") or "98232701")
SYNC_DAYS = int(os.environ.get("LIME_GCC_COUNTERS_DAYS") or "7")

COLUMNS = (
    "date", "source", "country", "channel", "subchannel",
    "campaign_id", "campaign_name", "visits", "users", "orders", "revenue",
)
DELETE_SQL = "DELETE FROM lime_gcc_counter_daily WHERE date >= %s AND date <= %s"
INSERT_SQL = f"INSERT INTO lime_gcc_counter_daily ({', '.join(COLUMNS)}) VALUES %s"

# Служебные значения GA4-измерений = «нет данных» (как в gcc_ga4).
_GA4_NOT_SET = {"", "(not set)", "(none)", "(direct)", "(organic)", "(referral)", "(not provided)"}

# Домен GA4 hostName → страна дашборда (как gcc_ga4._HOST_COUNTRY_RU).
_HOST_COUNTRY_RU = {
    "ae": "ОАЭ", "sa": "Саудовская Аравия", "qa": "Катар", "kw": "Кувейт", "om": "Оман",
}


def _clean(v: str | None) -> str:
    v = (v or "").strip()
    return "" if v in _GA4_NOT_SET else v


def metrika_rows(frm: str, to: str) -> list[tuple]:
    """Визиты/пользователи Метрики GCC, свёрнутые в кортежи COLUMNS (orders/revenue = 0)."""
    token = os.environ["GCC_METRICA_TOKEN"]
    raw = fetch_metrika_traffic(COUNTER_ID, token, frm, to)
    agg: dict[tuple, list[float]] = {}
    for r in raw:
        if not r.get("country"):
            continue
        channel, subchannel, _ = map_metrika_channel(
            r.get("traffic_source"), r.get("source_engine"), r.get("utm_source"))
        key = (r["date"], "metrika", r["country"], channel, subchannel,
               _clean(r.get("campaign")), "")
        cur = agg.setdefault(key, [0.0, 0.0])
        cur[0] += float(r.get("visits") or 0)
        cur[1] += float(r.get("users") or 0)
    return [key + (v[0], v[1], 0, 0) for key, v in agg.items()]


def ga4_order_rows(frm: str, to: str) -> list[tuple]:
    """Ecommerce-заказы/выручка GA4 по дате×стране×кампании, кортежи COLUMNS (visits/users = 0)."""
    raw = run_report(
        GA4_PROPERTY, frm, to,
        dimensions=["date", "hostName", "sessionSource", "sessionMedium",
                    "sessionCampaignId", "sessionCampaignName"],
        metrics=("ecommercePurchases", "purchaseRevenue"),
    )
    agg: dict[tuple, list[float]] = {}
    for r in raw:
        d, host, src, med, cid, cname = r["dims"]
        country = _HOST_COUNTRY_RU.get((host or "").strip().lower().split(".")[0])
        if not country:
            continue
        orders = float(r["metrics"][0] or 0)
        revenue = float(r["metrics"][1] or 0)
        if orders <= 0 and revenue <= 0:
            continue
        channel, subchannel, _ = map_ga4_channel(_clean(src), _clean(med))
        iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else d
        key = (iso, "ga4", country, channel, subchannel, _clean(cid), _clean(cname))
        cur = agg.setdefault(key, [0.0, 0.0])
        cur[0] += orders
        cur[1] += revenue
    return [key + (0, 0, v[0], v[1]) for key, v in agg.items()]


def run() -> None:
    frm = os.environ.get("LIME_GCC_COUNTERS_FROM")
    to = os.environ.get("LIME_GCC_COUNTERS_TO")
    if not frm or not to:
        end = date.today() - timedelta(days=1)
        frm = (end - timedelta(days=SYNC_DAYS - 1)).isoformat()
        to = end.isoformat()

    rows = metrika_rows(frm, to) + ga4_order_rows(frm, to)
    n_m = sum(1 for r in rows if r[1] == "metrika")
    n_g = len(rows) - n_m
    visits = sum(r[7] for r in rows)
    orders = sum(r[9] for r in rows)
    print(f"lime_gcc_counters {frm}..{to}: metrika={n_m} строк ({visits:.0f} визитов), "
          f"ga4={n_g} строк ({orders:.0f} заказов)")

    if os.environ.get("LIME_GCC_COUNTERS_DRY_RUN"):
        print("DRY RUN — БД не тронута")
        return

    # Prisma pooler URI (?pgbouncer=true) чистит db._database_url — psycopg2 параметр не понимает.
    conn = psycopg2.connect(_database_url(), connect_timeout=30)
    try:
        with conn, conn.cursor() as cur:
            # Идемпотентная миграция таблицы — тем же прогоном (паттерн lime_ru_metrika).
            with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "migrations", "lime", "024_gcc_counter_daily.sql"),
                      encoding="utf-8") as f:
                cur.execute(f.read())
            cur.execute(DELETE_SQL, (frm, to))
            psycopg2.extras.execute_values(cur, INSERT_SQL, rows, page_size=500)
        print(f"записано {len(rows)} строк")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
