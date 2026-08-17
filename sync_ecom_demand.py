#!/usr/bin/env python3
"""Синк e-com спроса Wordstat по всем наборам (slug × kind) из ECOM_PHRASE_SETS.

WORDSTAT_FROM=YYYY-MM-DD → бэкфилл с даты; иначе инкремент последних недель.
Пропуск, если нет YANDEX_SEARCHAPI_KEY. Запуск: python sync_ecom_demand.py
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
        print("ecom-demand: пропуск (нет YANDEX_SEARCHAPI_KEY)")
        return

    from sync.ecom_demand import (
        ECOM_PHRASE_SETS,
        ecom_daily_demand_up_to_date,
        ecom_demand_up_to_date,
        sync_ecom_wordstat_demand,
        sync_ecom_wordstat_demand_daily,
    )
    from sync.wordstat import daily_floor

    today = dt.date.today().isoformat()
    backfill = os.environ.get("WORDSTAT_FROM")

    for slug, kinds in ECOM_PHRASE_SETS.items():
        for kind, phrases in kinds.items():
            tag = f"{slug}/{kind}"
            # Недельный. Отдельный try на набор — падение одного не роняет остальные.
            try:
                if not backfill and ecom_demand_up_to_date(slug, kind):
                    print(f"ecom-demand[{tag}]: закрытая неделя уже есть — пропуск")
                else:
                    frm = backfill or (dt.date.today() - dt.timedelta(weeks=INCREMENTAL_WEEKS)).isoformat()
                    n = sync_ecom_wordstat_demand(slug, kind, phrases, frm, today)
                    print(f"ecom-demand[{tag}]: {n} строк week×phrase (с {frm})")
            except Exception as e:  # noqa: BLE001
                print(f"ecom-demand[{tag}]: ОШИБКА недельного: {e}")
            # Дневной (окно 60 дней, WORDSTAT_FROM не нужен).
            try:
                if ecom_daily_demand_up_to_date(slug, kind):
                    print(f"ecom-demand-daily[{tag}]: свежий день уже есть — пропуск")
                else:
                    nd = sync_ecom_wordstat_demand_daily(slug, kind, phrases, daily_floor(), today)
                    print(f"ecom-demand-daily[{tag}]: {nd} строк day×phrase")
            except Exception as e:  # noqa: BLE001
                print(f"ecom-demand-daily[{tag}]: ОШИБКА дневного: {e}")

    print("=== ecom demand sync DONE ===")


if __name__ == "__main__":
    main()
