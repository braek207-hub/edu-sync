# -*- coding: utf-8 -*-
"""sync/lime_roistat_report.py — визиты и продажи Роистата (LIME) → Google-таблица.

Отдельная задача с собственным кроном (5:00 МСК), НЕ связана с lime_kz_roistat →
Supabase: у отчёта своя судьба и свои поломки, их проще чинить в изоляции.

Считает по дню:
  • визиты платные / бесплатные / всего — free/paid берём из roistat_channels.map_*;
  • продажи (оплаченные заказы) и заявки по дате — из тех же дневных строк fetch_day.

Пишет в книгу LIME (вкладки Fact Traffic / Fact Orders) сопоставлением по ДАТЕ:
строки будущих дат в книге уже созданы, поэтому находим строку дня и обновляем
ячейки, а не добавляем новую.

Режимы (LIME_REPORT_MODE):
  probe (default) — только читает и печатает структуру книги + агрегат Роистата,
                    ничего не пишет. Первый прогон, чтобы снять контракт таблицы.
  write           — обновляет ячейки за окно дней.

ENV: ROISTAT_API_KEY, ROISTAT_PROJECT_ID (default 235593),
     LIME_REPORTS_SA_JSON (ключ сервис-аккаунта), LIME_REPORT_SHEET_ID,
     LIME_REPORT_DAYS_BACK (default 3), LIME_REPORT_FROM/LIME_REPORT_TO (бэкфилл).
"""
from __future__ import annotations

import os
import re
from datetime import date, timedelta

from sync.roistat_api import fetch_day
from sync.roistat_channels import PAID, map_roistat_channel

# Дата в книге отформатирована как «Сб 04.07.2026» — тянем из неё DD.MM.YYYY.
_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")

# Заголовок колонки → ключ агрегата. «сайт» и «общий» = итог (органический+платный),
# пишем в оба; если какой-то из них формула — шаг записи его не тронет.
_TRAFFIC_TARGETS = {
    "Трафик органический": "free_visits",
    "Трафик платный": "paid_visits",
    "Трафик сайт": "total_visits",
    "Трафик общий": "total_visits",
}

_RU_WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# Колонки вкладок при построении с нуля (режим build) — как на скрине заказчика.
_TRAFFIC_HEADER = ["Дата", "Трафик органический", "Трафик платный",
                   "Трафик сайт", "Трафик общий", "Год", "Месяц"]
_ORDERS_HEADER = ["Дата", "Продажи платный", "Продажи бесплатный",
                  "Продажи всего", "Год", "Месяц"]

# Старт истории для build (env LIME_REPORT_FROM переопределяет). Крон каждый день
# перестраивает окно [BUILD_FROM; вчера] целиком — идемпотентно, всегда полное.
BUILD_FROM = os.environ.get("LIME_REPORT_FROM") or "2026-07-01"


def _date_label(iso: str) -> str:
    """ISO-дата → «Сб 04.07.2026» как в книге заказчика."""
    d = date.fromisoformat(iso)
    return f"{_RU_WD[d.weekday()]} {d.strftime('%d.%m.%Y')}"

PROJECT = os.environ.get("ROISTAT_PROJECT_ID") or "235593"
SHEET_ID = os.environ.get("LIME_REPORT_SHEET_ID") or "1H6gSLOMDZDvGhvzD7mIvctlOmwPgdCk4WKXM8gtZ7X0"
DAYS_BACK = int(os.environ.get("LIME_REPORT_DAYS_BACK") or "3")

TRAFFIC_TAB = os.environ.get("LIME_REPORT_TRAFFIC_TAB") or "Fact Traffic"
ORDERS_TAB = os.environ.get("LIME_REPORT_ORDERS_TAB") or "Fact Orders"


def aggregate_day(api_rows: list[dict]) -> dict:
    """Свернуть дневные строки Роистата в визиты/продажи с делением платный/бесплатный.

    traffic_type строки берём из той же таксономии, что и склейка в Supabase, — чтобы
    «платный» здесь и «Платный» в дашборде значили одно и то же.

    Returns:
        paid_visits, free_visits, total_visits, paid_orders, free_orders, total_orders,
        paid_leads_count, free_leads_count, total_leads — числа (int).
    """
    agg = {
        "paid_visits": 0, "free_visits": 0,
        "paid_orders": 0, "free_orders": 0,
        "paid_leads_count": 0, "free_leads_count": 0,
    }
    for r in api_rows:
        _, _, traffic_type = map_roistat_channel(
            r["channel"], r.get("level2", ""), r.get("level2_id", "")
        )
        bucket = "paid" if traffic_type == PAID else "free"
        agg[f"{bucket}_visits"] += int(r.get("visits") or 0)
        agg[f"{bucket}_orders"] += int(r.get("paid_leads") or 0)
        agg[f"{bucket}_leads_count"] += int(r.get("leads") or 0)

    agg["total_visits"] = agg["paid_visits"] + agg["free_visits"]
    agg["total_orders"] = agg["paid_orders"] + agg["free_orders"]
    agg["total_leads"] = agg["paid_leads_count"] + agg["free_leads_count"]
    return agg


def _dump(rows: list[list], label: str, n: int = 4) -> None:
    print(f"\n=== {label} (первые {n} строк) ===")
    for i, row in enumerate(rows[:n]):
        print(f"  [{i}] {row}")


def probe(service) -> None:
    """Прочитать и напечатать структуру книги + СВЕРКУ агрегата Роистата с книгой.

    Ничего не пишет. Две цели:
      1) снять контракт (имена вкладок/колонок, какие ячейки формулы, формат даты);
      2) сверить пайплайн у получателя: за окно дат печатает Roistat vs текущее в книге,
         чтобы на уже заполненных вручную датах убедиться, что числа сходятся.
    """
    from sync.sheets_write import list_tabs, read_values

    print("Вкладки книги:", list_tabs(service, SHEET_ID))
    for tab in (TRAFFIC_TAB, ORDERS_TAB):
        try:
            vals = read_values(service, SHEET_ID, f"{tab}!A1:N12", render="FORMATTED_VALUE")
            _dump(vals, f"{tab} FORMATTED", 12)
            formulas = read_values(service, SHEET_ID, f"{tab}!A1:N10", render="FORMULA")
            _dump(formulas, f"{tab} FORMULA", 10)
        except Exception as e:  # noqa: BLE001 — probe печатает и идёт дальше
            print(f"  !! {tab}: {type(e).__name__}: {e}")

    key = os.environ["ROISTAT_API_KEY"]
    try:
        grid, _, hdr_i, name_to_col, row_of = _traffic_layout(service)
    except Exception as e:  # noqa: BLE001
        print(f"\n!! Сверка невозможна ({type(e).__name__}: {e})")
        return

    print("\n=== Сверка Roistat ↔ книга (визиты) ===")
    print("  дата        | Roistat платн/беспл/всего | книга платн/беспл/всего")
    for iso in _sync_dates():
        agg = aggregate_day(fetch_day(iso, PROJECT, key))
        ri = row_of.get(iso)
        cur = "нет строки"
        if ri is not None:
            def val(hdr: str) -> str:
                cj = name_to_col.get(hdr)
                row = grid[ri]
                return str(row[cj]) if cj is not None and cj < len(row) else "—"
            cur = f"{val('Трафик платный')}/{val('Трафик органический')}/{val('Трафик сайт')}"
        print(f"  {iso} | {agg['paid_visits']}/{agg['free_visits']}/{agg['total_visits']}"
              f" | {cur}")


def _col_letter(idx0: int) -> str:
    """0-based индекс колонки → буква A1 (0→A, 26→AA)."""
    s, n = "", idx0 + 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _norm_header(cell) -> str:
    """Заголовок из книги: убрать переносы и схлопнуть пробелы."""
    return re.sub(r"\s+", " ", str(cell or "").replace("\n", " ")).strip()


def _header_row(grid: list[list]) -> int | None:
    for i, row in enumerate(grid):
        if any(_norm_header(c) == "Дата" for c in row):
            return i
    return None


def _sync_dates() -> list[str]:
    """Окно дат ISO: [FROM;TO] если заданы, иначе последние DAYS_BACK дней до вчера."""
    frm_env, to_env = os.environ.get("LIME_REPORT_FROM"), os.environ.get("LIME_REPORT_TO")
    if frm_env and to_env:
        frm, to = date.fromisoformat(frm_env), date.fromisoformat(to_env)
    else:
        to = date.today() - timedelta(days=1)
        frm = to - timedelta(days=DAYS_BACK - 1)
    out, d = [], frm
    while d <= to:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _traffic_layout(service):
    """Разобрать TRAFFIC_TAB: (grid, formulas, hdr_i, name_to_col, row_of).

    grid — FORMATTED значения, formulas — те же ячейки render=FORMULA (для защиты от
    затирания формул). row_of: ISO-дата → 0-based индекс строки грида.
    """
    from sync.sheets_write import read_values

    rng = f"{TRAFFIC_TAB}!A1:Z400"
    grid = read_values(service, SHEET_ID, rng, render="FORMATTED_VALUE")
    formulas = read_values(service, SHEET_ID, rng, render="FORMULA")

    hdr_i = _header_row(grid)
    if hdr_i is None:
        raise RuntimeError(f"{TRAFFIC_TAB}: не нашёл строку заголовков с колонкой «Дата»")
    name_to_col = {_norm_header(c): j for j, c in enumerate(grid[hdr_i])}
    date_col = name_to_col.get("Дата", 0)

    row_of: dict[str, int] = {}
    for i in range(hdr_i + 1, len(grid)):
        row = grid[i]
        m = _DATE_RE.search(row[date_col] if date_col < len(row) else "")
        if m:
            row_of[f"{m[3]}-{m[2]}-{m[1]}"] = i  # 0-based индекс строки грида
    return grid, formulas, hdr_i, name_to_col, row_of


def write_traffic(service, day_to_agg: dict[str, dict]) -> int:
    """Обновить визиты в TRAFFIC_TAB сопоставлением по дате. Формулы не трогаем."""
    from sync.sheets_write import batch_update

    _, formulas, _, name_to_col, row_of = _traffic_layout(service)

    def is_formula(ri: int, cj: int) -> bool:
        if ri < len(formulas) and cj < len(formulas[ri]):
            return str(formulas[ri][cj]).startswith("=")
        return False

    updates, missing = [], []
    for iso, agg in day_to_agg.items():
        ri = row_of.get(iso)
        if ri is None:
            missing.append(iso)
            continue
        for hdr, key in _TRAFFIC_TARGETS.items():
            cj = name_to_col.get(hdr)
            if cj is None or is_formula(ri, cj):
                continue
            updates.append((f"{TRAFFIC_TAB}!{_col_letter(cj)}{ri + 1}", agg[key]))

    n = batch_update(service, SHEET_ID, updates)
    if missing:
        print(f"write_traffic: нет строк в книге для дат {missing} — пропущены")
    print(f"write_traffic: обновлено ячеек {n} за {len(day_to_agg) - len(missing)} дн.")
    return n


def _build_dates() -> list[str]:
    """Окно build: [BUILD_FROM; TO] (TO = LIME_REPORT_TO или вчера)."""
    to_env = os.environ.get("LIME_REPORT_TO")
    to = date.fromisoformat(to_env) if to_env else date.today() - timedelta(days=1)
    d, out = date.fromisoformat(BUILD_FROM), []
    while d <= to:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def build_book(service, dates: list[str]) -> None:
    """Построить вкладки Fact Traffic / Fact Orders с нуля и залить весь блок.

    Тестовая книга нам принадлежит целиком, поэтому пишем заголовок+строки одним
    блоком (без дате-матча и защиты формул — их тут нет). Идемпотентно: окно только
    растёт, поэтому перезапись от BUILD_FROM каждый раз даёт полную актуальную книгу.
    """
    from sync.sheets_write import add_tab, list_tabs, write_block

    key = os.environ["ROISTAT_API_KEY"]
    aggs = {d: aggregate_day(fetch_day(d, PROJECT, key)) for d in dates}

    tabs = set(list_tabs(service, SHEET_ID))
    for tab in (TRAFFIC_TAB, ORDERS_TAB):
        if tab not in tabs:
            add_tab(service, SHEET_ID, tab)
            print(f"создана вкладка «{tab}»")

    traffic = [_TRAFFIC_HEADER]
    orders = [_ORDERS_HEADER]
    for iso in dates:
        a, dt = aggs[iso], date.fromisoformat(iso)
        label = _date_label(iso)
        traffic.append([label, a["free_visits"], a["paid_visits"],
                        a["total_visits"], a["total_visits"], dt.year, dt.month])
        orders.append([label, a["paid_orders"], a["free_orders"],
                       a["total_orders"], dt.year, dt.month])
        print(f"{iso}: визиты платн={a['paid_visits']} беспл={a['free_visits']} "
              f"всего={a['total_visits']} | продажи платн={a['paid_orders']} "
              f"беспл={a['free_orders']} всего={a['total_orders']}")

    write_block(service, SHEET_ID, f"{TRAFFIC_TAB}!A1", traffic)
    write_block(service, SHEET_ID, f"{ORDERS_TAB}!A1", orders)
    print(f"\nзаписано: {TRAFFIC_TAB} и {ORDERS_TAB} — {len(dates)} дн. ({dates[0]}…{dates[-1]})")


def main() -> None:
    from sync.sheets_write import get_write_service

    mode = os.environ.get("LIME_REPORT_MODE") or "build"
    service = get_write_service()

    if mode == "probe":
        probe(service)
    elif mode == "build":
        build_book(service, _build_dates())
    elif mode == "write":
        key = os.environ["ROISTAT_API_KEY"]
        day_to_agg = {d: aggregate_day(fetch_day(d, PROJECT, key)) for d in _sync_dates()}
        write_traffic(service, day_to_agg)
    else:
        raise SystemExit(f"lime_roistat_report: неизвестный режим {mode!r}")


if __name__ == "__main__":
    main()
