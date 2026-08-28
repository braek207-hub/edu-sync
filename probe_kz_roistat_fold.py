"""Что даст исправленный build_rows на реальных ответах API — без записи и без БД.

Сравнение с витриной делается отдельно (`npm run duck` в Panda-BI): здесь печатается
CSV «день, строк, метрики», чтобы приложить его к числам витрины. Расход кабинетных
каналов берётся из БД, которой тут нет, поэтому колонка cost — только по некабинетным
каналам; для сверки годятся строки и аддитивные метрики API.

Запуск: python probe_kz_roistat_fold.py 2026-08-20 2026-08-24
"""
import os
import sys
from datetime import date, timedelta

from dotenv import load_dotenv

load_dotenv()

from sync.lime_kz_roistat import (COLUMNS, PROJECT, build_cohort_map, build_rows)
from sync.roistat_api import fetch_day
from sync.fx import to_rub as fx_to_rub

I = {c: i for i, c in enumerate(COLUMNS)}
METRICS = ["sessions", "purchases_count", "purchases_revenue", "cohort_orders"]


def main() -> None:
    frm, to = date.fromisoformat(sys.argv[1]), date.fromisoformat(sys.argv[2])
    key = os.environ["ROISTAT_API_KEY"]
    cohort_map = build_cohort_map(frm, to, key)

    print("date,api_rows,folded_rows," + ",".join(METRICS))
    day = frm
    while day <= to:
        day_s = day.isoformat()
        api_rows = fetch_day(day_s, PROJECT, key)
        rows = build_rows(api_rows, fx_to_rub("KZT", day_s), {}, day_s, cohort_map)
        vals = [sum(r[I[m]] or 0 for r in rows) for m in METRICS]
        print(f"{day_s},{len(api_rows)},{len(rows)},"
              + ",".join(f"{v:.2f}" if isinstance(v, float) else str(v) for v in vals))
        day += timedelta(days=1)


if __name__ == "__main__":
    main()
