# -*- coding: utf-8 -*-
"""РАЗОВЫЙ probe: структура вкладки «Traffic management 25 KZ NEW» книги отчёта LIME.

Цель — увидеть раскладку недельного плана по каналам (строки-блоки Traffic/Order/
CROSS SALES/Conversion/AOV/Budget, колонки недель OP/Fact/vs), чтобы написать синк
план → lime_plan_*. Только чтение, ничего не пишет. Удалить после использования.
"""
import os
import re

from sync.sheets_write import get_write_service, list_tabs, read_values

SHEET_ID = os.environ.get("LIME_REPORT_SHEET_ID") or "1H6gSLOMDZDvGhvzD7mIvctlOmwPgdCk4WKXM8gtZ7X0"
TAB = os.environ.get("PROBE_TAB") or "Traffic management 25 KZ NEW"


def col_letter(idx: int) -> str:
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def main() -> None:
    service = get_write_service()
    tabs = list_tabs(service, SHEET_ID)
    print("Вкладки книги:", tabs)
    if TAB not in tabs:
        print(f"!!! Вкладки «{TAB}» нет в книге {SHEET_ID}")
        return

    # 1) Левая колонка подписей строк (структура блоков).
    labels = read_values(service, SHEET_ID, f"{TAB}!A1:C140", render="FORMATTED_VALUE")
    print("--- A1:C140 (подписи строк) ---")
    for i, row in enumerate(labels, start=1):
        vals = [str(v) for v in row if str(v).strip()]
        if vals:
            print(f"r{i}: {' | '.join(vals)}")

    # 2) Шапка: строки 1–6 целиком — найти колонки недель.
    head = read_values(service, SHEET_ID, f"{TAB}!1:6", render="FORMATTED_VALUE")
    print("--- шапка: непустые ячейки строк 1–6 ---")
    week_cols = []
    for ri, row in enumerate(head, start=1):
        cells = [(ci, str(v).strip()) for ci, v in enumerate(row) if str(v).strip()]
        # компактно: первые 80 непустых
        print(f"r{ri}:", " ".join(f"{col_letter(ci)}={v[:20]}" for ci, v in cells[:80]))
        for ci, v in cells:
            if re.fullmatch(r"\d{1,2}", v) and 1 <= int(v) <= 53:
                week_cols.append((int(v), ci, ri))

    # 3) Блок W33 (и соседний контекст): 8 колонок от колонки недели 33.
    w33 = [c for c in week_cols if c[0] == 33]
    print("кандидаты недельных колонок (неделя, col, row):", week_cols[:60])
    if w33:
        _, ci, _ = w33[0]
        rng = f"{TAB}!{col_letter(max(ci - 2, 0))}1:{col_letter(ci + 5)}140"
        print(f"--- блок вокруг недели 33: {rng} ---")
        grid = read_values(service, SHEET_ID, rng, render="FORMATTED_VALUE")
        for i, row in enumerate(grid, start=1):
            vals = [str(v) for v in row]
            if any(v.strip() for v in vals):
                print(f"r{i}: {vals}")


if __name__ == "__main__":
    main()
