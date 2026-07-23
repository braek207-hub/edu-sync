#!/usr/bin/env python3
"""Синк BJORN «Спрос рынка» (Wordstat, по-фразно).

WORDSTAT_FROM=YYYY-MM-DD → бэкфилл с даты; иначе инкремент последних недель.
Пропуск, если нет YANDEX_SEARCHAPI_KEY. Запуск: python sync_bjorn_demand.py
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
        print("bjorn-demand: пропуск (нет YANDEX_SEARCHAPI_KEY)")
        return

    from sync.bjorn_demand import sync_bjorn_wordstat_demand
    from sync.wordstat import demand_up_to_date

    # Крон ежедневный: пока прошлой закрытой недели нет — дёргаем API; как появилась — пропуск.
    if not os.environ.get("WORDSTAT_FROM") and demand_up_to_date("bjorn_wordstat_demand"):
        print("bjorn-demand: последняя закрытая неделя уже есть — пропуск (до закрытия новой)")
        return

    frm = os.environ.get("WORDSTAT_FROM") or (
        dt.date.today() - dt.timedelta(weeks=INCREMENTAL_WEEKS)
    ).isoformat()
    n = sync_bjorn_wordstat_demand(frm, dt.date.today().isoformat())
    print(f"bjorn-demand: {n} строк week×phrase (с {frm})")
    print("=== bjorn demand sync DONE ===")


if __name__ == "__main__":
    main()
