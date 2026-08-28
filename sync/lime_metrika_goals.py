# -*- coding: utf-8 -*-
"""sync/lime_metrika_goals.py — все цели счётчика LIME → витрина lime_metrika_goal_daily.

Зачем отдельно от lime_ru_metrika. Тот тянет ЧЕТЫРЕ цели, зашитые константами
(корзина, чекаут и т.п.), — они часть e-com воронки и живут колонками витрины.
В счётчике при этом заведены регистрация, авторизация, цели Mindbox и автоцели
Директа; их состав меняется без нашего участия, и колонка на цель не масштабируется.
Поэтому здесь ДЛИННАЯ витрина: строка = (день × разрез × цель).

Состав целей не зашит: синк спрашивает его у Management API каждым прогоном и
складывает в справочник lime_metrika_goals. Появилась цель в счётчике — она
появится в витрине без правок кода.

Нули не пишем. Цели в счётчике десятками, разрезов — сотни: полный декартов
продукт раздул бы витрину на порядок ради строк, которые читатель всё равно
трактует как «нет данных».

ENV: DATABASE_URL, LIME_METRIKA_TOKEN, LIME_METRIKA_COUNTER_ID (default 23504302),
LIME_METRIKA_GOALS_DAYS_BACK (default 30), LIME_METRIKA_GOALS_FROM/TO (бэкфилл),
LIME_METRIKA_GOALS_DRY_RUN (не писать в БД).
Запуск: python -m sync.lime_metrika_goals
"""
import os
from datetime import date, timedelta

import psycopg2
import psycopg2.extras

from sync.lime_metrika_goals_api import fetch_goal_catalog, fetch_goal_reaches
from sync.metrika_channels import map_metrika_channel

COUNTER_ID = os.environ.get("LIME_METRIKA_COUNTER_ID") or "23504302"
DAYS_BACK = int(os.environ.get("LIME_METRIKA_GOALS_DAYS_BACK") or "30")

COLUMNS = ("date", "channel", "subchannel", "traffic_type", "campaign_id", "goal_id", "reaches")

INSERT_SQL = f"INSERT INTO lime_metrika_goal_daily ({', '.join(COLUMNS)}) VALUES %s"
DELETE_SQL = "DELETE FROM lime_metrika_goal_daily WHERE date >= %s AND date <= %s"

CATALOG_SQL = """
INSERT INTO lime_metrika_goals (goal_id, name, type, source, is_retargeting, synced_at)
VALUES %s
ON CONFLICT (goal_id) DO UPDATE SET
  name = EXCLUDED.name,
  type = EXCLUDED.type,
  source = EXCLUDED.source,
  is_retargeting = EXCLUDED.is_retargeting,
  synced_at = now()
"""

# Витрины создаёт сам синк (как lime_appmetrica): workflow применяет миграции, но
# порядок «миграция → синк» не гарантирован при ручном запуске.
_DDL = (
    """
    CREATE TABLE IF NOT EXISTS lime_metrika_goals (
      goal_id        text PRIMARY KEY,
      name           text NOT NULL DEFAULT '',
      type           text NOT NULL DEFAULT '',
      source         text NOT NULL DEFAULT '',
      is_retargeting boolean NOT NULL DEFAULT false,
      synced_at      timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lime_metrika_goal_daily (
      date         date NOT NULL,
      channel      text NOT NULL,
      subchannel   text NOT NULL DEFAULT '',
      traffic_type text NOT NULL DEFAULT '',
      campaign_id  text NOT NULL DEFAULT '',
      goal_id      text NOT NULL,
      reaches      integer NOT NULL DEFAULT 0,
      updated_at   timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (date, channel, subchannel, campaign_id, goal_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_lime_metrika_goal_daily_goal "
    "ON lime_metrika_goal_daily (goal_id, date)",
    "CREATE INDEX IF NOT EXISTS idx_lime_metrika_goal_daily_campaign "
    "ON lime_metrika_goal_daily (campaign_id, date)",
)


def build_rows(goal_rows, date_s: str) -> list[tuple]:
    """Свернуть достижения целей за день в кортежи порядка COLUMNS.

    Ключ свёртки — (channel, subchannel, campaign_id, goal_id): тот же разрез, по
    которому дашборд обогащает витрину PROCONTEXT (реклама — по campaign_id, прочее —
    по каналу/подканалу).

    Args:
        goal_rows: строки parse_goal_rows за date_s.
        date_s: дата строк YYYY-MM-DD.

    Returns:
        Список кортежей в порядке COLUMNS; строки с нулём достижений отброшены.
    """
    agg: dict[tuple[str, str, str, str], list] = {}

    for g in goal_rows:
        reaches = float(g.get("reaches") or 0)
        if reaches <= 0:
            continue
        channel, subchannel, traffic_type = map_metrika_channel(
            g.get("traffic_source"), g.get("source_engine")
        )
        campaign_id = (g.get("utm_campaign") or "").strip()
        goal_id = str(g.get("goal_id") or "").strip()
        if not goal_id:
            continue
        key = (channel, subchannel, campaign_id, goal_id)
        acc = agg.get(key)
        if acc is None:
            agg[key] = [traffic_type, reaches]
        else:
            acc[1] += reaches

    return [
        (date_s, channel, subchannel, traffic_type, campaign_id, goal_id, int(round(reaches)))
        for (channel, subchannel, campaign_id, goal_id), (traffic_type, reaches) in agg.items()
    ]


def group_by_day(goal_rows) -> dict[str, list[dict]]:
    """Строки ответа за период → {'YYYY-MM-DD': [строки этого дня]}.

    Строки без даты отбрасываются: писать их некуда, а в «сегодня» они бы соврали.
    """
    out: dict[str, list[dict]] = {}
    for g in goal_rows:
        day = (g.get("date") or "").strip()
        if not day:
            continue
        out.setdefault(day, []).append(g)
    return out


def _write_catalog(conn, catalog: list[dict]) -> None:
    if not catalog:
        return
    rows = [
        (g["goal_id"], g["name"], g["type"], g.get("source") or "", g["is_retargeting"])
        for g in catalog
    ]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur, CATALOG_SQL, rows, page_size=500,
            template="(%s, %s, %s, %s, %s, now())",
        )
    conn.commit()


def _sync_range(frm: date, to: date, conn) -> int:
    token = os.environ["LIME_METRIKA_TOKEN"]
    catalog = fetch_goal_catalog(COUNTER_ID, token)
    goal_ids = [g["goal_id"] for g in catalog]
    print(f"lime_metrika_goals: целей в счётчике {COUNTER_ID} — {len(goal_ids)}")
    if not goal_ids:
        return 0

    if conn is not None:
        _write_catalog(conn, catalog)

    # Весь период — одним обращением на пачку целей: день приходит измерением.
    # Посуточный цикл на 110 целях давал 7 запросов × число дней, и Метрика рубила
    # прогон квотой на середине месяца.
    api_rows = fetch_goal_reaches(COUNTER_ID, token, frm.isoformat(), to.isoformat(), goal_ids)
    by_day = group_by_day(api_rows)

    total = 0
    day = frm
    while day <= to:
        day_s = day.isoformat()
        rows = build_rows(by_day.get(day_s, []), day_s)

        if conn is None:
            i_r = COLUMNS.index("reaches")
            print(f"lime_metrika_goals: [DRY-RUN] {day_s} → {len(rows)} строк "
                  f"(достижений={sum(r[i_r] for r in rows)})")
        else:
            with conn.cursor() as cur:
                cur.execute(DELETE_SQL, (day_s, day_s))
                if rows:
                    psycopg2.extras.execute_values(cur, INSERT_SQL, rows, page_size=1000)
            conn.commit()
            print(f"lime_metrika_goals: {day_s} → {len(rows)} строк")

        total += len(rows)
        day += timedelta(days=1)
    return total


def sync_lime_metrika_goals() -> int:
    frm_env = os.environ.get("LIME_METRIKA_GOALS_FROM")
    to_env = os.environ.get("LIME_METRIKA_GOALS_TO")
    if frm_env and to_env:
        frm, to = date.fromisoformat(frm_env), date.fromisoformat(to_env)
    else:
        to = date.today()
        frm = to - timedelta(days=DAYS_BACK - 1)

    if os.environ.get("LIME_METRIKA_GOALS_DRY_RUN") or not os.environ.get("DATABASE_URL"):
        return _sync_range(frm, to, None)

    conn = psycopg2.connect(os.environ["DATABASE_URL"].split("?")[0], connect_timeout=30)
    try:
        with conn.cursor() as cur:
            for stmt in _DDL:
                cur.execute(stmt)
        conn.commit()
        return _sync_range(frm, to, conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    sync_lime_metrika_goals()
