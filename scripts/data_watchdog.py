# -*- coding: utf-8 -*-
"""Сторож свежести витрин: снимает состояние базы и падает, если данные протухли.

    python -m scripts.data_watchdog            # проверить всё
    python -m scripts.data_watchdog --dry      # только показать, код возврата 0
    python -m scripts.data_watchdog --dash lime

Ненулевой код возврата — это и есть уведомление: упавший workflow GitHub присылает
письмо, а строка WARN в логе успешного прогона не доходит ни до кого (проверено:
crm_leads стояли четыре дня при зелёных прогонах, см. sync/data_freshness.py).

Правило запроса: одна выборка на витрину, только COUNT и MAX по индексированной
колонке даты — сторож не должен сам становиться нагрузкой.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

from dotenv import load_dotenv

from sync.data_freshness import SOURCES, Source, SourceState, check_sources
from sync.db import get_connection

load_dotenv()  # локальный прогон читает .env; в Actions переменная приходит из секретов

WINDOW_DAYS = 10  # хватает на позавчера + неделю базовой линии


def _fetch_state(cur, source: Source, today: date) -> SourceState:
    col = source.date_column
    since = today - timedelta(days=WINDOW_DAYS)
    # Имена таблиц и колонок — из реестра в коде, не из ввода; подставляем как есть,
    # параметризовать идентификаторы psycopg2 всё равно не умеет.
    slice_and = f" AND {source.where}" if source.where else ""
    cur.execute(  # noqa: S608
        f"SELECT max({col})::date FROM {source.table}"
        + (f" WHERE {source.where}" if source.where else "")
    )
    row = cur.fetchone()
    max_date = row[0] if row else None

    cur.execute(  # noqa: S608
        f"SELECT {col}::date AS d, count(*) FROM {source.table} "
        f"WHERE {col} >= %s{slice_and} GROUP BY 1",
        (since,),
    )
    rows_by_day = {r[0]: int(r[1]) for r in cur.fetchall()}
    return SourceState(source, max_date, rows_by_day)


def main() -> int:
    parser = argparse.ArgumentParser(description="Сторож свежести витрин Panda-BI")
    parser.add_argument("--dash", help="проверить только один дашборд")
    parser.add_argument(
        "--dry",
        action="store_true",
        help="показать состояние, но не падать (для ручного прогона)",
    )
    args = parser.parse_args()

    today = date.today()
    sources = [s for s in SOURCES if not args.dash or s.dashboard == args.dash]
    if not sources:
        print(f"Нет витрин для дашборда {args.dash!r}")
        return 2

    states: list[SourceState] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            for source in sources:
                states.append(_fetch_state(cur, source, today))

    print(f"Свежесть витрин на {today}\n")
    print(f"{'дашборд':<12} {'таблица':<28} {'макс. дата':<12} {'отставание':>10} {'вчера':>8}")
    for state in states:
        src = state.source
        lag = "—" if state.max_date is None else f"{(today - state.max_date).days} дн."
        yesterday = state.rows_by_day.get(today - timedelta(days=1), 0)
        print(
            f"{src.dashboard:<12} {src.name:<28} "
            f"{str(state.max_date or '—'):<12} {lag:>10} {yesterday:>8}"
        )

    findings = check_sources(states, today)
    if not findings:
        print("\nВсё свежо.")
        return 0

    print("")
    for f in findings:
        mark = "СТОП" if f.is_critical else "ВНИМАНИЕ"
        print(f"{mark}: {f.source.dashboard}/{f.source.name} — {f.message}")

    critical = [f for f in findings if f.is_critical]
    if not critical:
        print("\nТолько предупреждения — прогон не роняем.")
        return 0

    print(
        f"\nДанные протухли: {len(critical)} витрин(ы). Это письмо и есть уведомление —"
        " смотреть надо источник, а не синк: он мог отработать успешно и записать"
        " ровно то, что ему дали."
    )
    return 0 if args.dry else 1


if __name__ == "__main__":
    sys.exit(main())
