# -*- coding: utf-8 -*-
"""Запись синка проектов в базу: источники, окна дат в пяти фактовых таблицах, статусы.

Ничего здесь не коммитит: транзакцию «источник × окно» ведёт вызывающий.
"""
from dataclasses import dataclass, fields

from psycopg2 import sql
from psycopg2.extras import Json, execute_batch

from sync.projects.direct_fetch import DirectRow
from sync.projects.metrika_fetch import GoalRow, ProductRow, PurchaseRow, TrafficRow

LOCK_CLASS = 7311

# таблица -> (класс строки, колонка даты для DELETE окна)
_TABLES = {
    "project_direct_daily": (DirectRow, "date"),
    "project_metrika_traffic": (TrafficRow, "date"),
    "project_metrika_goals": (GoalRow, "date"),
    "project_metrika_products": (ProductRow, "date"),
    "project_metrika_purchases": (PurchaseRow, "purchase_date"),
}
_PURCHASES = "project_metrika_purchases"
_WINDOW_TABLES = tuple(t for t in _TABLES if t != _PURCHASES)


@dataclass
class Source:
    id: int
    slug: str
    kind: str
    external_id: str
    account_id: int
    settings: dict
    status: str
    backfill: dict | None


_SELECT = (
    "SELECT id, dashboard_slug AS slug, kind, external_id, yandex_account_id AS account_id,"
    " settings, status, backfill FROM project_sources"
)


def _load(conn, where: str, slug: str | None) -> list[Source]:
    q = f"{_SELECT} WHERE {where}"
    args: list = []
    if slug is not None:
        q += " AND dashboard_slug = %s"
        args.append(slug)
    q += " ORDER BY id"
    with conn.cursor() as cur:
        cur.execute(q, args)
        return [Source(*r) for r in cur.fetchall()]


def load_daily_sources(conn, slug: str | None = None) -> list[Source]:
    """Источники для ночного прогона.

    reconnect берём: статус снимается переподключением аккаунта, а mark_ok вернёт источнику ok.
    Но только при ya.status = 'ok' — иначе каждую ночь впустую стучимся с мёртвым токеном.
    Источник без загруженной истории (last_synced_at IS NULL) ночью не трогаем: 7 дней
    без остальной истории и mark_ok оставили бы дыру, которую уже не видно по статусу.
    Порядок — по последней попытке (updated_at двигают и mark_ok, и mark_error): упавший ночью
    источник уходит в хвост и не отнимает бюджет у здоровых каждую ночь.
    """
    q = (
        "SELECT ps.id, ps.dashboard_slug AS slug, ps.kind, ps.external_id,"
        " ps.yandex_account_id AS account_id, ps.settings, ps.status, ps.backfill"
        " FROM project_sources ps JOIN yandex_accounts ya ON ya.id = ps.yandex_account_id"
        " WHERE ps.status IN ('ok','error','reconnect') AND ya.status = 'ok'"
        " AND ps.last_synced_at IS NOT NULL"
    )
    args: list = []
    if slug is not None:
        q += " AND ps.dashboard_slug = %s"
        args.append(slug)
    q += " ORDER BY ps.updated_at, ps.id"
    with conn.cursor() as cur:
        cur.execute(q, args)
        return [Source(*r) for r in cur.fetchall()]


def load_queued_sources(conn, slug: str | None = None) -> list[Source]:
    return _load(conn, "backfill->>'state' IN ('queued','running')", slug)


def try_lock_source(conn, source_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s, %s)", (LOCK_CLASS, source_id))
        return bool(cur.fetchone()[0])


def unlock_source(conn, source_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(%s, %s)", (LOCK_CLASS, source_id))


def _columns(table: str) -> list[str]:
    return ["source_id", "dashboard_slug"] + [f.name for f in fields(_TABLES[table][0])]


def _values(source: Source, row) -> tuple:
    return (source.id, source.slug) + tuple(getattr(row, f.name) for f in fields(row))


def _delete(cur, table: str, source: Source, date_from: str, date_to: str) -> None:
    cur.execute(
        sql.SQL("DELETE FROM {t} WHERE source_id = %s AND {d} BETWEEN %s AND %s").format(
            t=sql.Identifier(table), d=sql.Identifier(_TABLES[table][1])
        ),
        (source.id, date_from, date_to),
    )


def _insert_sql(table: str) -> sql.Composed:
    cols = _columns(table)
    return sql.SQL("INSERT INTO {t} ({c}) VALUES ({p})").format(
        t=sql.Identifier(table),
        c=sql.SQL(", ").join(map(sql.Identifier, cols)),
        p=sql.SQL(", ").join(sql.Placeholder() * len(cols)),
    )


def replace_window(conn, table: str, source: Source, date_from: str, date_to: str, rows: list) -> int:
    """Пустой список тоже стирает окно: Метрика вернула 0 строк = данных за период нет."""
    if table not in _WINDOW_TABLES:
        raise ValueError(f"unknown table: {table}")
    with conn.cursor() as cur:
        _delete(cur, table, source, date_from, date_to)
        if rows:
            execute_batch(cur, _insert_sql(table), [_values(source, r) for r in rows], page_size=500)
    return len(rows)


def replace_purchases(conn, source: Source, date_from: str, date_to: str, rows: list[PurchaseRow]) -> int:
    sets = [c for c in _columns(_PURCHASES) if c not in ("source_id", "order_id", "purchase_date")]
    upsert = _insert_sql(_PURCHASES) + sql.SQL(
        " ON CONFLICT (source_id, order_id, purchase_date) DO UPDATE SET {s}, updated_at = now()"
    ).format(s=sql.SQL(", ").join(sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c)) for c in sets))
    with conn.cursor() as cur:
        _delete(cur, _PURCHASES, source, date_from, date_to)
        if rows:
            execute_batch(cur, upsert, [_values(source, r) for r in rows], page_size=500)
    return len(rows)


def mark_ok(conn, source_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE project_sources SET status = 'ok', last_synced_at = now(), last_error = NULL,"
            " updated_at = now() WHERE id = %s",
            (source_id,),
        )


def mark_error(conn, source_id: int, message: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE project_sources SET status = 'error', last_error = %s, updated_at = now() WHERE id = %s",
            (message, source_id),
        )


def save_backfill(conn, source_id: int, backfill: dict, status: str | None = None) -> None:
    with conn.cursor() as cur:
        if status is None:
            cur.execute(
                "UPDATE project_sources SET backfill = %s, updated_at = now() WHERE id = %s",
                (Json(backfill), source_id),
            )
        else:
            cur.execute(
                "UPDATE project_sources SET backfill = %s, status = %s, updated_at = now() WHERE id = %s",
                (Json(backfill), status, source_id),
            )
