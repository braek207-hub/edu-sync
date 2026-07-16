#!/usr/bin/env python3
"""Синк EDU «Спрос рынка» (Wordstat, по-фразно).

WORDSTAT_FROM=YYYY-MM-DD → бэкфилл с даты; иначе инкремент последних недель.
Пропуск, если нет YANDEX_SEARCHAPI_KEY. Запуск: python sync_edu_demand.py
"""
import datetime as dt
import os
import sys

from dotenv import load_dotenv

load_dotenv()

INCREMENTAL_WEEKS = 8


def main() -> None:
    if not os.environ.get("DATABASE_URL"):
        print("ОШИБКА: нет DATABASE_URL")
        sys.exit(1)
    if not os.environ.get("YANDEX_SEARCHAPI_KEY"):
        print("edu-demand: пропуск (нет YANDEX_SEARCHAPI_KEY)")
        return

    from sync.edu_demand import sync_edu_wordstat_demand

    frm = os.environ.get("WORDSTAT_FROM") or (
        dt.date.today() - dt.timedelta(weeks=INCREMENTAL_WEEKS)
    ).isoformat()
    n = sync_edu_wordstat_demand(frm, dt.date.today().isoformat())
    print(f"edu-demand: {n} строк week×phrase (с {frm})")
    print("=== edu demand sync DONE ===")


if __name__ == "__main__":
    main()
