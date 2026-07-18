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
from sync.gcc_google_geo import fetch_geo_spend
from sync.gcc_metrika import fetch_metrika_traffic
from sync.gcc_triplewhale import aggregate_orders_by_channel, fetch_tw_orders, fetch_tw_spend, spend_by_channel

METRICA_COUNTER_ID = os.environ.get("GCC_METRICA_COUNTER_ID") or "98232701"
SYNC_DAYS = int(os.environ.get("LIME_GCC_SYNC_DAYS") or "7")

DELETE_SQL = "DELETE FROM lime_stats WHERE region = 'gcc' AND date >= %s AND date <= %s"

# Порядок колонок = порядок полей в кортежах merge_rows(). Держим одним списком, чтобы
# добавление колонки не разъезжалось с индексами в сводке dry-run.
COLUMNS = (
    "date", "data_source", "region", "country", "channel", "subchannel", "traffic_type",
    "campaign_id", "campaign_name",
    "cost", "clicks", "impressions", "sessions", "users", "clients",
    "purchases_count", "purchases_revenue", "customers",
    "new_users", "new_customers", "new_customers_revenue",
    "bounce_rate", "page_depth", "cart_reaches", "checkout_reaches",
)

INSERT_SQL = f"INSERT INTO lime_stats ({', '.join(COLUMNS)}) VALUES %s"


def merge_rows(metrika_rows, tw_order_rows, tw_spend_rows, fx_rate, date_s,
               rub_spend_rows=()) -> list[tuple]:
    """Свернуть трафик (Метрика) + заказы/расход (Triple Whale) по (country, channel, subchannel).

    Страна берётся из самого источника: Метрика — домен витрины, TW-заказы — journey.
    Источники без гео-разбивки (расход из TW summary-page — он на весь магазин) дают
    country=None: такие строки не приписываются ни одной стране, но входят в GCC-тотал.

    Args:
        metrika_rows: sync.gcc_metrika.fetch_metrika_traffic() за день date_s.
        tw_order_rows: sync.gcc_triplewhale.aggregate_orders_by_channel() за день date_s.
        tw_spend_rows: sync.gcc_triplewhale.spend_by_channel() за день date_s (в AED).
        fx_rate: курс AED→RUB (sync.fx.to_rub("AED", date_s)).
        date_s: дата строк (YYYY-MM-DD).
        rub_spend_rows: расход, УЖЕ пересчитанный в рубли (гео-расход Google из кабинета,
            sync.gcc_google_geo) — курс к нему не применяется повторно.

    Returns:
        Список кортежей в порядке COLUMNS, по одному на (country, channel, subchannel).
    """
    agg: dict[tuple[str | None, str, str, str], dict] = {}

    def _bucket(country, campaign, channel, subchannel, traffic_type):
        # Кампания в ключе: id одинаков в utm Метрики, attribution TW и кабинете Google,
        # поэтому визиты, заказы и расход одной кампании сходятся в одну строку.
        key = (country, campaign or "", channel, subchannel)
        row = agg.get(key)
        if row is None:
            row = {
                "traffic_type": traffic_type,
                "campaign_name": "",
                "sessions": 0,
                "users": 0,
                "orders": 0,
                "revenue": 0.0,
                "cost": 0.0,
                "cost_rub": 0.0,
                "new_users": 0,
                # Взвешенные на визиты — иначе среднее от средних соврёт при склейке строк.
                "bounce_w": 0.0,
                "depth_w": 0.0,
                "cart": 0,
                "checkout": 0,
            }
            agg[key] = row
        elif not row["traffic_type"]:
            row["traffic_type"] = traffic_type
        return row

    for m in metrika_rows:
        channel, subchannel, traffic_type = map_metrika_channel(m["traffic_source"], m["source_engine"])
        row = _bucket(m.get("country"), m.get("campaign"), channel, subchannel, traffic_type)
        row["sessions"] += int(m["visits"] or 0)
        row["users"] += int(m["users"] or 0)
        row["new_users"] += int(m.get("new_users") or 0)
        row["bounce_w"] += float(m.get("bounce_w") or 0)
        row["depth_w"] += float(m.get("depth_w") or 0)
        row["cart"] += int(m.get("cart_reaches") or 0)
        row["checkout"] += int(m.get("checkout_reaches") or 0)

    for o in tw_order_rows:
        row = _bucket(o.get("country"), o.get("campaign"), o["channel"], o["subchannel"],
                      o.get("traffic_type"))
        row["orders"] += int(o["orders"] or 0)
        row["revenue"] += float(o["revenue"] or 0)

    for sp in tw_spend_rows:
        # summary-page даёт расход на магазин целиком (country=None); гео-разбивка Meta — Фаза 2.
        row = _bucket(sp.get("country"), None, sp["channel"], sp["subchannel"],
                      sp.get("traffic_type"))
        row["cost"] += float(sp["cost"] or 0)

    for sp in rub_spend_rows:
        # Уже в рублях (гео-расход Google из кабинета) — в отдельную корзину, мимо курса.
        row = _bucket(sp.get("country"), sp.get("campaign_id"), sp["channel"], sp["subchannel"],
                      sp.get("traffic_type"))
        row["cost_rub"] += float(sp["cost"] or 0)
        # Имя кампании знает только кабинет — Метрика и TW отдают голый id.
        if sp.get("campaign_name") and not row["campaign_name"]:
            row["campaign_name"] = sp["campaign_name"]

    out: list[tuple] = []
    for (country, campaign, channel, subchannel), row in agg.items():
        cost_rub = round(row["cost"] * fx_rate + row["cost_rub"], 2)
        revenue_rub = round(row["revenue"] * fx_rate, 2)
        sessions = row["sessions"]
        out.append((
            date_s, "web", "gcc", country, channel, subchannel, row["traffic_type"],
            campaign, row["campaign_name"],                    # campaign_id, campaign_name
            cost_rub, 0, 0, row["sessions"], row["users"], 0,   # cost, clicks, impressions, sessions, users, clients
            row["orders"], revenue_rub, 0,                      # purchases_count, purchases_revenue, customers
            row["new_users"], 0, 0.0,                           # new_users, new_customers, new_customers_revenue
            # Средневзвешенные по визитам; проценты и «страниц за визит» — конвенция
            # polinarepik_metrica_visits, хендлер взвешивает обратно (SUM(x * sessions)).
            round(row["bounce_w"] / sessions * 100, 4) if sessions else None,
            round(row["depth_w"] / sessions, 4) if sessions else None,
            row["cart"], row["checkout"],
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
        tw_metrics = fetch_tw_spend(tw_key, shop, day_s)
        # Расход Google берём из кабинета (разложен по странам), если Script там уже стоит;
        # тогда ga_adCost из TW выбрасываем — иначе один и тот же расход посчитается дважды.
        google_geo = fetch_geo_spend(conn, day_s)
        if google_geo:
            tw_metrics = {k: v for k, v in tw_metrics.items() if k != "ga_adCost"}
        spend = spend_by_channel(tw_metrics, day_s)
        fx_rate = fx_to_rub("AED", day_s)
        rows = merge_rows(metrika, orders, spend, fx_rate, day_s, rub_spend_rows=google_geo)

        if conn is None:
            i_country = COLUMNS.index("country")
            i_cost = COLUMNS.index("cost")
            i_revenue = COLUMNS.index("purchases_revenue")
            i_orders = COLUMNS.index("purchases_count")
            i_sessions = COLUMNS.index("sessions")
            cost_sum = sum(r[i_cost] for r in rows)
            revenue_sum = sum(r[i_revenue] for r in rows)
            orders_sum = sum(r[i_orders] for r in rows)
            print(f"lime_gcc: [DRY-RUN] {day_s} → {len(rows)} строк "
                  f"(cost={cost_sum:.2f}₽, revenue={revenue_sum:.2f}₽, orders={orders_sum})")
            by_country: dict[str | None, list[float]] = {}
            for r in rows:
                acc = by_country.setdefault(r[i_country], [0, 0.0, 0, 0.0])
                acc[0] += r[i_sessions]
                acc[1] += r[i_revenue]
                acc[2] += r[i_orders]
                acc[3] += r[i_cost]
            for country, (sessions, revenue, orders_n, cost) in sorted(
                by_country.items(), key=lambda kv: -kv[1][0]
            ):
                print(f"    {str(country or '(тотал GCC)'):<22} визиты={sessions:<7} "
                      f"заказы={orders_n:<5} выручка={revenue:.0f}₽ расход={cost:.0f}₽")
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
