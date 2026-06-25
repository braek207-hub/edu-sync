"""Экспорт CSV для ручной загрузки офлайн-конверсий VUZ в Яндекс.Метрику.

Использование:
  set GOOGLE_APPLICATION_CREDENTIALS=sa.json
  set GOOGLE_SHEETS_ID=...
  python scripts/export_metrika_vuz_csv.py
  python scripts/export_metrika_vuz_csv.py --days 21 --out exports/vuz_offline_21d.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sync.metrika_offline import (  # noqa: E402
    COUNTER_VUZ,
    GOAL_CONNECTION,
    GOAL_DEAL,
    GOAL_PAYMENT,
    _collect_conversions,
)
from sync.sheets import get_sheets_service  # noqa: E402
from sync.utils import to_iso_date  # noqa: E402

MSK = ZoneInfo("Europe/Moscow")


def _event_date_from_ts(ts: int) -> date | None:
    return datetime.fromtimestamp(ts, tz=MSK).date()


def _end_of_day_ts(d: date) -> int:
    dt = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=MSK)
    return int(dt.timestamp())


def export_vuz_csv(*, days: int, out_path: Path) -> dict:
    if not os.environ.get("GOOGLE_SHEETS_ID") and os.environ.get("SHEET_ID_EDU"):
        os.environ["GOOGLE_SHEETS_ID"] = os.environ["SHEET_ID_EDU"]

    sid = os.environ.get("GOOGLE_SHEETS_ID", "").strip()
    if not sid:
        raise SystemExit("GOOGLE_SHEETS_ID (или SHEET_ID_EDU) не задан")

    today = date.today()
    date_from = today - timedelta(days=days - 1)

    service = get_sheets_service()
    convs = _collect_conversions(service, sid)

    # VUZ + последние N дней по дате события; DateTime = конец дня МСК (лучше привязка)
    rows: list[tuple[str, str, int, float]] = []
    seen: set[tuple[str, str]] = set()
    stats = {"connection": 0, "deal": 0, "payment": 0, "skipped_dedup": 0, "skipped_date": 0}

    for counter, cid, target, ts, price, _lead in convs:
        if counter != COUNTER_VUZ:
            continue
        event_d = _event_date_from_ts(ts)
        if not event_d or event_d < date_from or event_d > today:
            stats["skipped_date"] += 1
            continue
        key = (cid, target)
        if key in seen:
            stats["skipped_dedup"] += 1
            continue
        seen.add(key)
        rows.append((cid, target, _end_of_day_ts(event_d), price))
        if target in stats:
            stats[target] += 1

    rows.sort(key=lambda r: (r[2], r[1], r[0]))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ClientId", "Target", "DateTime", "Price", "Currency"])
        for cid, target, ts, price in rows:
            w.writerow([cid, target, ts, (f"{price:.2f}" if price else ""), ("RUB" if price else "")])

    stats["total"] = len(rows)
    stats["period"] = f"{date_from.isoformat()} — {today.isoformat()}"
    stats["counter"] = COUNTER_VUZ
    stats["out"] = str(out_path)
    return stats


def main() -> None:
    p = argparse.ArgumentParser(description="Экспорт офлайн-конверсий VUZ в CSV для Метрики")
    p.add_argument("--days", type=int, default=21, help="Окно в днях (по умолчанию 21)")
    p.add_argument(
        "--out",
        type=Path,
        default=ROOT / "exports" / f"vuz_offline_{date.today().isoformat()}_21d.csv",
    )
    args = p.parse_args()
    stats = export_vuz_csv(days=args.days, out_path=args.out)
    print(f"Период: {stats['period']}, счётчик {stats['counter']}")
    print(f"Строк: {stats['total']} (connection={stats['connection']}, deal={stats['deal']}, payment={stats['payment']})")
    print(f"Пропущено: дата={stats['skipped_date']}, дедуп={stats['skipped_dedup']}")
    print(f"Файл: {stats['out']}")


if __name__ == "__main__":
    main()
