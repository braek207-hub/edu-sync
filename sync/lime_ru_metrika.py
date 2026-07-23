# -*- coding: utf-8 -*-
"""sync/lime_ru_metrika.py — RU-срез Яндекс.Метрики → staging lime_metrika_campaign_ru.

НЕ пишет в lime_stats (в отличие от KZ-среза): основной RU-трафик уже даёт витрина
PROCONTEXT (region='ru'). Здесь собираем ПОВЕДЕНИЕ и POST-CLICK ВОРОНКУ Метрики по
каналу/кампании в отдельную витрину, которой дашборд ОБОГАЩАЕТ строки витрины по
(date, campaign_id) для рекламных и по (date, channel, subchannel) — для прочих.
Так поведение/воронка Метрики появляются на всех каналах основной таблицы, без
задвоения визитов (визиты остаются за PROCONTEXT).

Post-view — за Медиаметрикой (отдельный источник), сюда не входит.

Взвешенные накопители bounce_w/depth_w = Σ(показатель × визиты): аддитивны при любой
группировке, ставка на чтении = w / visits (как в GCC/KZ и в дашборде _mBounceW).

ENV: DATABASE_URL, LIME_METRIKA_TOKEN, LIME_METRIKA_COUNTER_ID (default 23504302),
LIME_RU_METRIKA_DAYS_BACK (default 30), LIME_RU_METRIKA_FROM/TO (бэкфилл),
LIME_RU_METRIKA_DRY_RUN (не писать в БД).
Запуск: python -m sync.lime_ru_metrika
"""
import os
from datetime import date, timedelta

import psycopg2
import psycopg2.extras

from sync.lime_ru_metrika_api import fetch_ru_traffic
from sync.metrika_channels import map_metrika_channel

COUNTER_ID = os.environ.get("LIME_METRIKA_COUNTER_ID") or "23504302"
DAYS_BACK = int(os.environ.get("LIME_RU_METRIKA_DAYS_BACK") or "30")

COLUMNS = (
    "date", "channel", "subchannel", "traffic_type", "campaign_id", "campaign_name",
    "visits", "users", "new_users", "bounce_w", "depth_w", "duration_w",
    "card_view", "look_image", "cart", "checkout", "orders", "revenue",
)

INSERT_SQL = f"INSERT INTO lime_metrika_campaign_ru ({', '.join(COLUMNS)}) VALUES %s"
DELETE_SQL = "DELETE FROM lime_metrika_campaign_ru WHERE date >= %s AND date <= %s"


def build_rows(metrika_rows, date_s: str) -> list[tuple]:
    """Свернуть строки Метрики за день в кортежи порядка COLUMNS.

    Ключ свёртки — (channel, subchannel, campaign_id, campaign_name). campaign_id берём
    из utm_campaign (для рекламы = id кампании Директа, совпадает с витриной). Нераспознанные
    каналы схлопываются до уровня канала (пустой campaign_id).

    Args:
        metrika_rows: строки parse_metrika_kz за date_s.
        date_s: дата строк YYYY-MM-DD.

    Returns:
        Список кортежей в порядке COLUMNS.
    """
    agg: dict[tuple[str, str, str, str], dict] = {}

    for m in metrika_rows:
        channel, subchannel, traffic_type = map_metrika_channel(
            m.get("traffic_source"), m.get("source_engine")
        )
        # campaign_id из utm_campaign: для рекламного трафика = id кампании Директа (как в
        # витрине PROCONTEXT), по нему дашборд и джойнит. Прочие каналы дают пустой id и
        # обогащаются по (channel, subchannel).
        campaign_id = (m.get("utm_campaign") or "").strip()
        campaign_name = (m.get("direct_campaign_name") or "").strip()
        key = (channel, subchannel, campaign_id, campaign_name)
        acc = agg.get(key)
        if acc is None:
            acc = {
                "traffic_type": traffic_type,
                "visits": 0.0, "users": 0.0, "new_users": 0.0,
                "bounce_w": 0.0, "depth_w": 0.0, "duration_w": 0.0,
                "card_view": 0.0, "look_image": 0.0,
                "cart": 0.0, "checkout": 0.0, "orders": 0.0, "revenue": 0.0,
            }
            agg[key] = acc

        visits = float(m.get("visits") or 0)
        acc["visits"] += visits
        acc["users"] += float(m.get("users") or 0)
        acc["new_users"] += float(m.get("new_users") or 0)
        acc["bounce_w"] += float(m.get("bounce_rate") or 0) * visits
        acc["depth_w"] += float(m.get("page_depth") or 0) * visits
        # Время на сайте — среднее (сек), взвешиваем по визитам: ставка = duration_w / visits.
        acc["duration_w"] += float(m.get("avg_duration") or 0) * visits
        acc["card_view"] += float(m.get("card_view") or 0)
        acc["look_image"] += float(m.get("look_image") or 0)
        acc["cart"] += float(m.get("cart_reaches") or 0)
        acc["checkout"] += float(m.get("checkout_reaches") or 0)
        acc["orders"] += float(m.get("orders") or 0)
        acc["revenue"] += float(m.get("revenue") or 0)

    out: list[tuple] = []
    for (channel, subchannel, campaign_id, campaign_name), acc in agg.items():
        out.append((
            date_s, channel, subchannel, acc["traffic_type"], campaign_id, campaign_name,
            int(acc["visits"]), int(acc["users"]), int(acc["new_users"]),
            round(acc["bounce_w"], 2), round(acc["depth_w"], 2), round(acc["duration_w"], 2),
            int(acc["card_view"]), int(acc["look_image"]),
            int(acc["cart"]), int(acc["checkout"]), int(acc["orders"]), round(acc["revenue"], 2),
        ))
    return out


def _sync_range(frm: date, to: date, conn) -> int:
    token = os.environ["LIME_METRIKA_TOKEN"]
    total = 0
    day = frm
    while day <= to:
        day_s = day.isoformat()
        metrika = fetch_ru_traffic(COUNTER_ID, token, day_s, day_s)
        rows = build_rows(metrika, day_s)

        if conn is None:
            i_v, i_o = COLUMNS.index("visits"), COLUMNS.index("orders")
            print(f"lime_ru_metrika: [DRY-RUN] {day_s} → {len(rows)} строк "
                  f"(визиты={sum(r[i_v] for r in rows)}, заказы={sum(r[i_o] for r in rows)})")
        else:
            with conn.cursor() as cur:
                cur.execute(DELETE_SQL, (day_s, day_s))
                if rows:
                    psycopg2.extras.execute_values(cur, INSERT_SQL, rows, page_size=500)
            conn.commit()
            print(f"lime_ru_metrika: {day_s} → {len(rows)} строк")

        total += len(rows)
        day += timedelta(days=1)
    return total


def sync_lime_ru_metrika() -> int:
    frm_env = os.environ.get("LIME_RU_METRIKA_FROM")
    to_env = os.environ.get("LIME_RU_METRIKA_TO")
    if frm_env and to_env:
        frm, to = date.fromisoformat(frm_env), date.fromisoformat(to_env)
    else:
        to = date.today()
        frm = to - timedelta(days=DAYS_BACK - 1)

    if os.environ.get("LIME_RU_METRIKA_DRY_RUN") or not os.environ.get("DATABASE_URL"):
        return _sync_range(frm, to, None)

    conn = psycopg2.connect(os.environ["DATABASE_URL"].split("?")[0], connect_timeout=30)
    try:
        return _sync_range(frm, to, conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    sync_lime_ru_metrika()
