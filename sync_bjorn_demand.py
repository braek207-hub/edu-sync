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

    from sync.bjorn_demand import sync_bjorn_wordstat_demand, sync_bjorn_wordstat_demand_daily
    from sync.wordstat import daily_demand_up_to_date, daily_floor, demand_up_to_date

    today = dt.date.today().isoformat()

    # Недельный спрос. Крон ежедневный: пока прошлой закрытой недели нет — дёргаем API;
    # как появилась — пропуск. Отдельный try: ошибка недельного не роняет дневной блок.
    try:
        if not os.environ.get("WORDSTAT_FROM") and demand_up_to_date("bjorn_wordstat_demand"):
            print("bjorn-demand: последняя закрытая неделя уже есть — пропуск (до закрытия новой)")
        else:
            frm = os.environ.get("WORDSTAT_FROM") or (
                dt.date.today() - dt.timedelta(weeks=INCREMENTAL_WEEKS)
            ).isoformat()
            n = sync_bjorn_wordstat_demand(frm, today)
            print(f"bjorn-demand: {n} строк week×phrase (с {frm})")
    except Exception as e:  # noqa: BLE001 — блоки независимы, падение одного не роняет другой
        print(f"bjorn-demand: ОШИБКА недельного синка: {e}")

    # Дневной спрос (скользящее окно 60 дней Wordstat). WORDSTAT_FROM не нужен:
    # окно всегда синкается целиком (идемпотентный upsert), глубже floor API не отдаёт.
    try:
        if daily_demand_up_to_date("bjorn_wordstat_demand_daily"):
            print("bjorn-demand-daily: свежий день уже есть — пропуск (до нового отставания)")
        else:
            nd = sync_bjorn_wordstat_demand_daily(daily_floor(), today)
            print(f"bjorn-demand-daily: {nd} строк day×phrase")
    except Exception as e:  # noqa: BLE001
        print(f"bjorn-demand-daily: ОШИБКА дневного синка: {e}")

    print("=== bjorn demand sync DONE ===")


if __name__ == "__main__":
    main()
