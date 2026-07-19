# -*- coding: utf-8 -*-
"""sync/lime_kz_roistat.py — KZ из Роистата → lime_stats (region='kz_roistat').

Третий взгляд на одни казахстанские продажи рядом с kz_metrika (Метрика) и kz (витрина
MySQL). Ценность именно этого источника:
  • видит ВСЕ заказы, включая 12% заведённых без веб-сессии (568 из 4 714 заявок июня);
  • различает заявку и оплаченную продажу → выкуп из данных (86.1%), а не константа 0.81;
  • даёт оплативших клиентов для CAC (3 089 за июнь) — Stat API Метрики их не отдаёт;
  • знает расход Meta, к кабинету которой у нас нет доступа, и расход КМС/PMax Google,
    которых недобирает наш Google Ads Script.

Сверка с интерфейсом Роистата на полном июне: расхождение 0.05–0.10% по всем метрикам.

Владение: только этот синк удаляет и пишет region='kz_roistat'. Регион ОБЯЗАН быть в
sync/lime.py:FOREIGN_REGIONS, иначе ежедневный прогон lime.py снесёт срез молча.

Валюта: тенге → рубли по курсу ЦБ на дату строки. Исключение — расход Яндекс.Директа:
Роистат отдаёт его в рублях, помеченных валютой проекта (июнь: Роистат 1 398 441 против
1 397 767 ₽ кабинета — одно число). Умножив на курс, занизили бы его в 6.6 раза, поэтому
расход Директа берём из кабинета, причём по campaign_id, чтобы он разложился по кампаниям.

ENV: DATABASE_URL, ROISTAT_API_KEY, ROISTAT_PROJECT_ID (default 235593),
LIME_KZ_ROISTAT_DAYS_BACK (default 30), LIME_KZ_ROISTAT_FROM/LIME_KZ_ROISTAT_TO (бэкфилл).
Запуск: python -m sync.lime_kz_roistat
"""
import os
from datetime import date, timedelta

import psycopg2
import psycopg2.extras

from sync.fx import to_rub as fx_to_rub
from sync.roistat_api import fetch_day
from sync.roistat_channels import campaign_of, map_roistat_channel

REGION = "kz_roistat"
PROJECT = os.environ.get("ROISTAT_PROJECT_ID") or "235593"
DAYS_BACK = int(os.environ.get("LIME_KZ_ROISTAT_DAYS_BACK") or "30")

# Каналы, чей расход в Роистате лежит в рублях под видом тенге (валютная ловушка выше) —
# берём его из кабинета по campaign_id. Значение — логин кабинета в lime_direct_stats.
CABINET_COST_CHANNELS = {"Яндекс.Директ 1": "LIME-KZ1"}

COLUMNS = (
    "date", "data_source", "region", "channel", "subchannel", "traffic_type",
    "campaign_id", "campaign_name",
    "cost", "clicks", "impressions", "sessions", "users", "clients",
    "purchases_count", "purchases_revenue", "customers",
    "new_users", "new_customers", "new_customers_revenue",
    "net_purchases_count", "net_revenue",
)

INSERT_SQL = f"INSERT INTO lime_stats ({', '.join(COLUMNS)}) VALUES %s"
DELETE_SQL = f"DELETE FROM lime_stats WHERE region = '{REGION}' AND date >= %s AND date <= %s"

SELECT_CABINET_COST = """
SELECT campaign_id::text, SUM(COALESCE(cost, 0))::float
FROM lime_direct_stats
WHERE client_login = %s AND date = %s::date
GROUP BY campaign_id
"""


def build_rows(api_rows, fx_rate: float, cabinet_cost: dict, date_s: str) -> list[tuple]:
    """Свернуть строки Роистата в кортежи lime_stats порядка COLUMNS.

    Справочник имён не нужен: campaign_id приходит прямо из Роистата (value уровня) и
    совпадает с id наших кабинетов один в один.

    Args:
        api_rows: строки sync.roistat_api.fetch_day.
        fx_rate: курс тенге к рублю на date_s.
        cabinet_cost: campaign_id → расход из кабинета в рублях за этот день.
        date_s: дата строк YYYY-MM-DD.

    Returns:
        Список кортежей в порядке COLUMNS.
    """
    out: list[tuple] = []
    for r in api_rows:
        name = r["channel"]
        channel, subchannel, traffic_type = map_roistat_channel(
            name, r.get("level2", ""), r.get("level2_id", "")
        )
        campaign_id, campaign_name = campaign_of(name, r)

        if name in CABINET_COST_CHANNELS:
            # Из Роистата этот расход брать нельзя (валюта), поэтому нет кампании в
            # кабинете → ноль. Ноль честнее числа, заниженного в 6.6 раза.
            cost = float(cabinet_cost.get(campaign_id, 0.0))
        else:
            cost = float(r["cost"]) * fx_rate

        gross_revenue = (float(r["paid_revenue"]) + float(r["progress_revenue"])
                         + float(r["canceled_revenue"])) * fx_rate
        visits = int(r["visits"])

        out.append((
            date_s, "web", REGION, channel, subchannel, traffic_type,
            campaign_id, campaign_name,
            round(cost, 2), 0.0, 0.0,
            # users=0, а НЕ visits: уникальных посетителей у Roistat API нет вовсе —
            # 48 метрик, и ни одной про посетителей (в интерфейсе колонка есть, наружу
            # не отдаётся). Приравняв их к визитам, я получил ровно 1.000 посетителя на
            # визит против настоящих 0.757 у Метрики — цифра выглядела как данные, но
            # была выдумкой. Ноль честнее: ячейка покажет прочерк, а пользователей для
            # свода берём у Метрики, которая их знает.
            visits, 0, 0,
            int(r["leads"]), round(gross_revenue, 2), int(r["paid_clients"]),
            0, 0, 0.0,
            int(r["paid_leads"]), round(float(r["paid_revenue"]) * fx_rate, 2),
        ))
    return out


def _cabinet_cost(conn, date_s: str) -> dict:
    """campaign_id → расход кабинетных каналов за день, в рублях."""
    if conn is None:
        return {}
    out: dict[str, float] = {}
    with conn.cursor() as cur:
        for login in set(CABINET_COST_CHANNELS.values()):
            cur.execute(SELECT_CABINET_COST, (login, date_s))
            for campaign_id, cost in cur.fetchall():
                out[str(campaign_id)] = out.get(str(campaign_id), 0.0) + float(cost or 0)
    return out


def _sync_range(frm: date, to: date, conn) -> int:
    key = os.environ["ROISTAT_API_KEY"]
    total = 0
    day = frm
    while day <= to:
        day_s = day.isoformat()
        api_rows = fetch_day(day_s, PROJECT, key)
        fx_rate = fx_to_rub("KZT", day_s)
        rows = build_rows(api_rows, fx_rate, _cabinet_cost(conn, day_s), day_s)

        if conn is None:
            i_o, i_r = COLUMNS.index("purchases_count"), COLUMNS.index("purchases_revenue")
            i_c = COLUMNS.index("cost")
            print(f"lime_kz_roistat: [DRY-RUN, БЕЗ БД: расход Директа не резолвится] {day_s} "
                  f"→ {len(rows)} строк (заявки={sum(r[i_o] for r in rows)}, "
                  f"выручка={sum(r[i_r] for r in rows):.0f}₽, "
                  f"расход={sum(r[i_c] for r in rows):.0f}₽)")
        else:
            with conn.cursor() as cur:
                cur.execute(DELETE_SQL, (day_s, day_s))
                if rows:
                    psycopg2.extras.execute_values(cur, INSERT_SQL, rows, page_size=500)
            conn.commit()
            print(f"lime_kz_roistat: {day_s} → {len(rows)} строк")

        total += len(rows)
        day += timedelta(days=1)
    return total


def sync_lime_kz_roistat() -> int:
    frm_env = os.environ.get("LIME_KZ_ROISTAT_FROM")
    to_env = os.environ.get("LIME_KZ_ROISTAT_TO")
    if frm_env and to_env:
        frm, to = date.fromisoformat(frm_env), date.fromisoformat(to_env)
    else:
        to = date.today()
        frm = to - timedelta(days=DAYS_BACK - 1)

    if not os.environ.get("DATABASE_URL"):
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
    sync_lime_kz_roistat()
