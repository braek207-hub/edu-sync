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
from sync.roistat_api import fetch_day, fetch_cohort
from sync.roistat_channels import campaign_of, map_roistat_channel

REGION = "kz_roistat"
PROJECT = os.environ.get("ROISTAT_PROJECT_ID") or "235593"
DAYS_BACK = int(os.environ.get("LIME_KZ_ROISTAT_DAYS_BACK") or "30")

# Когорта дозревает: заказы визитов оплачиваются недели спустя, поэтому окно синка (и пересбора)
# держим шире дневного. Дефолт 90 дней — за это время выкупается практически вся когорта.
COHORT_MATURE_DAYS = int(os.environ.get("LIME_KZ_ROISTAT_COHORT_DAYS") or "90")
# Когортную выгрузку (dim=daily × marker_level) на всё окно Роистат не отдаёт — падает с
# «Too many data to process» (проверено на 80 днях в Task 1). Тянем чанками по окну ОПЛАТЫ.
COHORT_CHUNK_DAYS = int(os.environ.get("LIME_KZ_ROISTAT_COHORT_CHUNK") or "14")

# Каналы, чей расход в Роистате лежит в рублях под видом тенге (валютная ловушка выше) —
# берём его из кабинета по campaign_id. Значение — логин кабинета в lime_direct_stats.
# Кабинет сам уже в рублях с НДС (тенге сконвертированы в sync/lime_direct.py), поэтому
# ниже cabinet-расход берётся как есть, без домножения на fx_rate.
CABINET_COST_CHANNELS = {"Яндекс.Директ 1": "LIME-KZ1"}

COLUMNS = (
    "date", "data_source", "region", "channel", "subchannel", "traffic_type",
    "campaign_id", "campaign_name",
    "cost", "clicks", "impressions", "sessions", "users", "clients",
    "purchases_count", "purchases_revenue", "customers",
    "new_users", "new_customers", "new_customers_revenue",
    "net_purchases_count", "net_revenue",
    "cohort_orders", "cohort_revenue", "cohort_new_sales", "cohort_repeat_sales",
    "cohort_leads", "cohort_clients",
    # Новизна по дате ОПЛАТЫ — параллельно когортной (по дате визита). Складывать нельзя:
    # это одни и те же продажи, разложенные по разным датам.
    "new_sales", "repeat_sales", "repeat_leads",
)

INSERT_SQL = f"INSERT INTO lime_stats ({', '.join(COLUMNS)}) VALUES %s"
DELETE_SQL = f"DELETE FROM lime_stats WHERE region = '{REGION}' AND date >= %s AND date <= %s"

SELECT_CABINET_COST = """
SELECT campaign_id::text, SUM(COALESCE(cost, 0))::float
FROM lime_direct_stats
WHERE client_login = %s AND date = %s::date
GROUP BY campaign_id
"""


def cohort_key(row: dict) -> tuple[str, str, str]:
    """Полная грань склейки: (campaign_id, channel, subchannel).

    subchannel обязателен — у одного канала несколько подканалов (SEO Google/Yandex/Others),
    и склейка без него задвоила бы когорту канала по всем его subchannel-строкам.
    """
    name = row["channel"]
    campaign_id, _ = campaign_of(name, row)
    _, subchannel, _ = map_roistat_channel(name, row.get("level2", ""), row.get("level2_id", ""))
    return (campaign_id, name, subchannel)


def build_rows(api_rows, fx_rate: float, cabinet_cost: dict, date_s: str, cohort_map: dict) -> list[tuple]:
    """Свернуть строки Роистата в кортежи lime_stats порядка COLUMNS.

    Справочник имён не нужен: campaign_id приходит прямо из Роистата (value уровня) и
    совпадает с id наших кабинетов один в один.

    Args:
        api_rows: строки sync.roistat_api.fetch_day.
        fx_rate: курс тенге к рублю на date_s.
        cabinet_cost: campaign_id → расход из кабинета в рублях за этот день.
        date_s: дата строк YYYY-MM-DD.
        cohort_map: (visit_date, campaign_id, channel, subchannel) → (orders, new, repeat,
            revenue_kzt, leads, clients), построен build_cohort_map за окно оплаты.

    Returns:
        Список кортежей в порядке COLUMNS.
    """
    # Свёртка по ключу витрины, а не запись строк API один в один. Несколько строк
    # Роистата после map_roistat_channel и campaign_of дают один и тот же кортеж —
    # и до 28.08.2026 они ложились в базу отдельными строками. Базовый запрос дашборда
    # группирует по тому же кортежу и СУММИРУЕТ, поэтому лишние строки не пропадали,
    # а удваивали числа: замер прода нашёл 1765 таких групп с 01.01.2025, из них 1002
    # полностью идентичных (+61 332 ₽ расхода, +2 351 ₽ выручки).
    #
    # Две вещи при свёртке складывать нельзя, и обе — не аддитивные по построению:
    #   • расход кабинетных каналов берётся по campaign_id, то есть у двух строк с одним
    #     id он ОДИН И ТОТ ЖЕ (отсюда и брались идентичные пары);
    #   • когорта лежит по своему ключу и при совпадении читается дважды.
    # Обе учитываются один раз — по множеству уже учтённых ключей.
    acc: dict[tuple, dict] = {}
    order: list[tuple] = []
    counted_cabinet: set[tuple] = set()
    counted_cohort: set[tuple] = set()

    for r in api_rows:
        name = r["channel"]
        channel, subchannel, traffic_type = map_roistat_channel(
            name, r.get("level2", ""), r.get("level2_id", "")
        )
        campaign_id, campaign_name = campaign_of(name, r)
        key = (channel, subchannel, traffic_type, campaign_id, campaign_name)

        if name in CABINET_COST_CHANNELS:
            # Из Роистата этот расход брать нельзя (валюта), поэтому нет кампании в
            # кабинете → ноль. Ноль честнее числа, заниженного в 6.6 раза.
            cab_key = (name, campaign_id)
            cost = 0.0 if cab_key in counted_cabinet else float(cabinet_cost.get(campaign_id, 0.0))
            counted_cabinet.add(cab_key)
        else:
            cost = float(r["cost"]) * fx_rate

        gross_revenue = (float(r["paid_revenue"]) + float(r["progress_revenue"])
                         + float(r["canceled_revenue"])) * fx_rate
        visits = int(r["visits"])

        cohort_key = (date_s, campaign_id, name, subchannel)
        cohort = cohort_map.get(cohort_key)
        if cohort_key in counted_cohort:
            cohort = None
        elif cohort:
            counted_cohort.add(cohort_key)
        c_orders = int(cohort[0]) if cohort else None
        c_new = int(cohort[1]) if cohort else None
        c_repeat = int(cohort[2]) if cohort else None
        c_rev = round(float(cohort[3]) * fx_rate, 2) if cohort else None
        c_leads = int(cohort[4]) if cohort else None
        c_clients = int(cohort[5]) if cohort else None

        if key not in acc:
            acc[key] = {"cost": 0.0, "visits": 0, "leads": 0, "gross": 0.0, "clients": 0,
                        "paid_leads": 0, "paid_revenue": 0.0, "new_sales": 0,
                        "repeat_sales": 0, "repeat_leads": 0,
                        "c_orders": None, "c_rev": None, "c_new": None,
                        "c_repeat": None, "c_leads": None, "c_clients": None}
            order.append(key)
        a = acc[key]
        a["cost"] += cost
        a["visits"] += visits
        a["leads"] += int(r["leads"])
        a["gross"] += gross_revenue
        a["clients"] += int(r["paid_clients"])
        a["paid_leads"] += int(r["paid_leads"])
        a["paid_revenue"] += float(r["paid_revenue"]) * fx_rate
        # .get: метрику могут отключить в проекте Роистата — тогда пишем 0, а не падаем.
        a["new_sales"] += int(r.get("new_sales") or 0)
        a["repeat_sales"] += int(r.get("repeat_sales") or 0)
        a["repeat_leads"] += int(r.get("repeat_leads") or 0)
        for dst, val in (("c_orders", c_orders), ("c_rev", c_rev), ("c_new", c_new),
                         ("c_repeat", c_repeat), ("c_leads", c_leads),
                         ("c_clients", c_clients)):
            if val is not None:
                a[dst] = (a[dst] or 0) + val

    out: list[tuple] = []
    for key in order:
        channel, subchannel, traffic_type, campaign_id, campaign_name = key
        a = acc[key]
        out.append((
            date_s, "web", REGION, channel, subchannel, traffic_type,
            campaign_id, campaign_name,
            round(a["cost"], 2), 0.0, 0.0,
            # users=0, а НЕ visits: уникальных посетителей у Roistat API нет вовсе —
            # 48 метрик, и ни одной про посетителей (в интерфейсе колонка есть, наружу
            # не отдаётся). Приравняв их к визитам, я получил ровно 1.000 посетителя на
            # визит против настоящих 0.757 у Метрики — цифра выглядела как данные, но
            # была выдумкой. Ноль честнее: ячейка покажет прочерк, а пользователей для
            # свода берём у Метрики, которая их знает.
            a["visits"], 0, 0,
            a["leads"], round(a["gross"], 2), a["clients"],
            0, 0, 0.0,
            a["paid_leads"], round(a["paid_revenue"], 2),
            a["c_orders"], (round(a["c_rev"], 2) if a["c_rev"] is not None else None),
            a["c_new"], a["c_repeat"], a["c_leads"], a["c_clients"],
            a["new_sales"], a["repeat_sales"], a["repeat_leads"],
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


def _merge_cohort_rows(cohort_map: dict, cohort_rows: list[dict]) -> None:
    """Аккумулировать когортные строки в map по полной грани — СУММА, не перезапись.

    Один visit_date получает заказы из РАЗНЫХ чанков окна оплаты: заказ визита D мог быть
    оплачен в любом последующем чанке. Перезапись потеряла бы заказы из прежних чанков.
    """
    for cr in cohort_rows:
        cid, ch, sub = cohort_key(cr)
        k = (cr["visit_date"], cid, ch, sub)
        o, n, rp, rev, ld, cl = cohort_map.get(k, (0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
        cohort_map[k] = (o + cr["cohort_orders"], n + cr["cohort_new"],
                         rp + cr["cohort_repeat"], rev + cr["cohort_revenue"],
                         ld + cr["cohort_leads"], cl + cr["cohort_clients"])


def build_cohort_map(frm: date, to: date, key: str) -> dict:
    """Когорта за окно оплаты [frm; to] чанками по COHORT_CHUNK_DAYS.

    Одна выгрузка на всё окно падает с «Too many data». `to` эксклюзивен у Роистата,
    поэтому конец окна — to+1.
    """
    cohort_map: dict[tuple[str, str, str, str], tuple[float, float, float, float]] = {}
    end_excl = to + timedelta(days=1)
    chunk_start = frm
    while chunk_start < end_excl:
        chunk_end = min(chunk_start + timedelta(days=COHORT_CHUNK_DAYS), end_excl)
        _merge_cohort_rows(cohort_map, fetch_cohort(chunk_start.isoformat(),
                                                    chunk_end.isoformat(), PROJECT, key))
        chunk_start = chunk_end
    return cohort_map


def _sync_range(frm: date, to: date, conn) -> int:
    key = os.environ["ROISTAT_API_KEY"]

    # Когорта по дате визита за всё окно оплаты, чанками (одна выгрузка не проходит).
    cohort_map = build_cohort_map(frm, to, key)

    total = 0
    day = frm
    while day <= to:
        day_s = day.isoformat()
        api_rows = fetch_day(day_s, PROJECT, key)
        fx_rate = fx_to_rub("KZT", day_s)
        rows = build_rows(api_rows, fx_rate, _cabinet_cost(conn, day_s), day_s, cohort_map)

        if conn is None:
            i_o, i_r = COLUMNS.index("purchases_count"), COLUMNS.index("purchases_revenue")
            i_c, i_co = COLUMNS.index("cost"), COLUMNS.index("cohort_orders")
            print(f"lime_kz_roistat: [DRY-RUN] {day_s} → {len(rows)} строк "
                  f"(заявки={sum(r[i_o] for r in rows)}, "
                  f"когорт.заказы={sum(r[i_co] or 0 for r in rows)}, "
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
        frm = to - timedelta(days=max(DAYS_BACK, COHORT_MATURE_DAYS) - 1)

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
