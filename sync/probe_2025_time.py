"""Task 0 (Фаза B): есть ли ВРЕМЯ в ячейках даты листа «Лиды 2025»? (разовый, НЕ прод).

Гейтит пользу бэкфилла created_ts для 2025 (обучающее окно = созревшие ≤ ~2026-01 =
в основном 2025). read_sheet по умолчанию отдаёт ФОРМАТИРОВАННОЕ значение — если ячейка
отформатирована «дата без времени», время скрыто, хотя в данных есть. Поэтому читаем и
UNFORMATTED_VALUE: datetime-ячейка = сериал-число, дробная часть > 0 → время присутствует.
"""

import os

from sync.sheets import get_sheets_service, read_sheet
from sync.utils import pick_index_loose, to_iso_datetime

_DATE_VARIANTS = ["date created", "дата создания", "дата", "date"]


def main():
    sid = os.environ["GOOGLE_SHEETS_ID"]
    svc = get_sheets_service()
    sheet = "Лиды 2025"

    formatted = read_sheet(svc, sid, sheet)
    unformatted = read_sheet(svc, sid, sheet, value_render_option="UNFORMATTED_VALUE")
    if not formatted:
        print(">>> лист пуст / не найден")
        return

    dcol = pick_index_loose(formatted[0], _DATE_VARIANTS)
    print(f"=== Лиды 2025: строк={len(formatted)-1}, колонка даты idx={dcol} ('{formatted[0][dcol] if dcol!=-1 else '?'}') ===")
    if dcol == -1:
        print(">>> колонка даты не найдена")
        return

    fmt_time, serial_time, checked = 0, 0, 0
    samples = []
    for i in range(1, min(len(formatted), 300)):
        frow = formatted[i]
        urow = unformatted[i] if i < len(unformatted) else []
        fcell = frow[dcol] if dcol < len(frow) else ""
        ucell = urow[dcol] if dcol < len(urow) else ""
        if not fcell:
            continue
        checked += 1
        ts = to_iso_datetime(fcell)
        if ts and (ts.hour or ts.minute or ts.second):
            fmt_time += 1
        if isinstance(ucell, (int, float)) and float(ucell) != int(float(ucell)):
            serial_time += 1
        if len(samples) < 5:
            samples.append((fcell, ucell))

    print(f"проверено ячеек: {checked}")
    print(f"formatted дал ВРЕМЯ (to_iso_datetime.hour/min/sec>0): {fmt_time}")
    print(f"UNFORMATTED сериал с дробной частью (время в данных): {serial_time}")
    print(f"примеры (formatted | unformatted): {samples}")
    print(">>> ВЕРДИКТ:", "ВРЕМЯ ЕСТЬ → бэкфилл created_ts 2025 имеет смысл (Task 5 в силе)"
          if (fmt_time > 0 or serial_time > 0)
          else "ВРЕМЕНИ НЕТ → 2025 created_ts не восстановить; выгода same-day только на 2026-хвосте")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    main()
