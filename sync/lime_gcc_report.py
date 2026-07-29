# -*- coding: utf-8 -*-
"""sync/lime_gcc_report.py — трафик и заказы GCC → Google-таблица (широкий формат).

Отдельная задача с собственным кроном, аналог lime_roistat_report для KZ, но источники
другие и формат широкий: ORG/PAID/Total по каждой стране Залива + ORG Total/PAID Total/Total
по всему GCC.

Источники (ФАЗА 1 — только WEB; APP по app 6299245 добавится ФАЗОЙ 2 в те же ячейки):
  • WEB трафик = sessions Метрики, WEB заказы = purchases Triple Whale — оба уже собраны
    в lime_stats (region='gcc', data_source='web') с колонками country + traffic_type.

Ячейка книги = web (+ app в Ф2). Считаем per (дата × страна) словарь {org, paid} и по
странам, и суммарно по GCC. «Total»-колонки GCC включают остаток country=NULL (заказы/
визиты без страны), поэтому по странам не сходятся с тоталом — так и задумано в данных.

Пишем ПО ИМЕНАМ колонок («ORG UAE», «PAID Total», …) — устойчиво к вставке/перестановке
столбцов. Формулы (напр. Total=ORG+PAID) на существующих строках не перезаписываем.

Режимы (LIME_GCC_MODE): refresh (default, последние N дней) | build (бэкфилл с нуля,
дописывает недостающие строки-даты) | probe (read-only дамп структуры + сверка).

ENV: DATABASE_URL, GSC_SA_KEY/GOOGLE_APPLICATION_CREDENTIALS (сервис-аккаунт lime-reports),
     LIME_GCC_SHEET_ID, LIME_GCC_REFRESH_DAYS (default 7),
     LIME_GCC_FROM/LIME_GCC_TO (окно build).
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone

import psycopg2

SHEET_ID = os.environ.get("LIME_GCC_SHEET_ID") or "1JSM7wcZlNnKX6uB4kk7UwkQzv7QEgPyyYUiOD-77WiE"
TRAFFIC_TAB = os.environ.get("LIME_GCC_TRAFFIC_TAB") or "Fact Traffic GCC"
ORDERS_TAB = os.environ.get("LIME_GCC_ORDERS_TAB") or "Fact Orders GCC"

REFRESH_DAYS = int(os.environ.get("LIME_GCC_REFRESH_DAYS") or "7")
BUILD_FROM = os.environ.get("LIME_GCC_FROM") or "2025-08-01"

# lime_stats хранит страну по-русски; книга — кодами Залива. Порядок = как в шаблоне.
_COUNTRY_CODE = {
    "ОАЭ": "UAE",
    "Саудовская Аравия": "KSA",
    "Катар": "QA",
    "Кувейт": "KW",
    "Оман": "OM",
    "Бахрейн": "BH",
}
_CODES = ("UAE", "KSA", "QA", "KW", "OM", "BH")

_RU_WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


def _msk_today() -> date:
    """Московская дата (UTC+3, без DST) — окно считаем по Москве, раннер в UTC."""
    return (datetime.now(timezone.utc) + timedelta(hours=3)).date()


def _date_label(iso: str) -> str:
    """ISO → «Сб 04.07.2026» как в книге (день недели по-русски + DD.MM.YYYY)."""
    d = date.fromisoformat(iso)
    return f"{_RU_WD[d.weekday()]} {d.strftime('%d.%m.%Y')}"


# ─────────────────────────── агрегация данных ───────────────────────────

# Пустой срез дня: org/paid по «Total» (весь GCC) и по каждой стране-коду.
def _empty_slice() -> dict:
    keys = ("Total",) + _CODES
    return {"org": {k: 0 for k in keys}, "paid": {k: 0 for k in keys}}


_WEB_SQL = """
SELECT date::text AS d, country, traffic_type,
       COALESCE(SUM(sessions), 0)::bigint        AS sessions,
       COALESCE(SUM(purchases_count), 0)::bigint AS orders
FROM lime_stats
WHERE region = 'gcc' AND data_source = 'web' AND date = ANY(%s)
GROUP BY date, country, traffic_type
"""


def fetch_web(conn, dates: list[str]) -> tuple[dict, dict]:
    """(traffic, orders): date → срез {org/paid × Total/страна} из lime_stats web.

    «Total» = весь GCC, включая country=NULL (визиты/заказы без страны); коды —
    только 6 узнаваемых стран. traffic_type «Платный»→paid, иначе→org.
    """
    traffic: dict[str, dict] = {d: _empty_slice() for d in dates}
    orders: dict[str, dict] = {d: _empty_slice() for d in dates}
    with conn.cursor() as cur:
        cur.execute(_WEB_SQL, (dates,))
        for d, country, ttype, sess, ords in cur.fetchall():
            bucket = "paid" if ttype == "Платный" else "org"
            code = _COUNTRY_CODE.get((country or "").strip())
            for metric_map, val in ((traffic, sess), (orders, ords)):
                slc = metric_map[d][bucket]
                slc["Total"] += int(val)
                if code:
                    slc[code] += int(val)
    return traffic, orders


# ─────────────────────────── запись в книгу ───────────────────────────

def _norm_header(cell) -> str:
    return re.sub(r"\s+", " ", str(cell or "").replace("\n", " ")).strip()


def _header_row(grid: list[list]) -> int | None:
    for i, row in enumerate(grid):
        if any(_norm_header(c) == "Дата" for c in row):
            return i
    return None


def _col_letter(idx0: int) -> str:
    s, n = "", idx0 + 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _tab_layout(service, tab: str):
    """(formulas, name_to_col, date_col, row_of, next_row0) — заголовок и колонки по ИМЕНАМ."""
    from sync.sheets_write import read_values

    rng = f"{tab}!A1:BZ2000"
    grid = read_values(service, SHEET_ID, rng, render="FORMATTED_VALUE")
    formulas = read_values(service, SHEET_ID, rng, render="FORMULA")
    hdr_i = _header_row(grid)
    if hdr_i is None:
        raise RuntimeError(f"{tab}: не нашёл строку заголовков с «Дата»")
    name_to_col = {_norm_header(c): j for j, c in enumerate(grid[hdr_i])}
    date_col = name_to_col["Дата"]
    row_of: dict[str, int] = {}
    last = hdr_i
    for i in range(hdr_i + 1, len(grid)):
        row = grid[i]
        m = _DATE_RE.search(row[date_col] if date_col < len(row) else "")
        if m:
            row_of[f"{m[3]}-{m[2]}-{m[1]}"] = i
            last = i
    return formulas, name_to_col, date_col, row_of, last + 1


def _slice_cells(tab, iso, slc, name_to_col, formulas, ri0, is_new):
    """Ячейки одного дня одной вкладки: ORG/PAID по Total и странам + Дата/Год/Месяц (новой)."""
    row1 = ri0 + 1

    def is_formula(cj):
        return (ri0 < len(formulas) and cj < len(formulas[ri0])
                and str(formulas[ri0][cj]).startswith("="))

    ups = []
    targets = [("ORG Total", slc["org"]["Total"]), ("PAID Total", slc["paid"]["Total"])]
    for code in _CODES:
        targets.append((f"ORG {code}", slc["org"][code]))
        targets.append((f"PAID {code}", slc["paid"][code]))
    # Total-колонки: пишем только если это НЕ формула (в шаблоне часто =ORG+PAID).
    total_targets = [("Total", slc["org"]["Total"] + slc["paid"]["Total"])]
    for code in _CODES:
        total_targets.append((f"Total {code}", slc["org"][code] + slc["paid"][code]))

    for hdr, val in targets:
        cj = name_to_col.get(hdr)
        if cj is None or (not is_new and is_formula(cj)):
            continue
        ups.append((f"{tab}!{_col_letter(cj)}{row1}", [[val]]))
    for hdr, val in total_targets:
        cj = name_to_col.get(hdr)
        if cj is None or is_formula(cj):  # Total трогаем только если это литерал
            continue
        ups.append((f"{tab}!{_col_letter(cj)}{row1}", [[val]]))
    if is_new:
        dt = date.fromisoformat(iso)
        for hdr, v in (("Дата", _date_label(iso)), ("Год", dt.year), ("Месяц", dt.month)):
            cj = name_to_col.get(hdr)
            if cj is not None:
                ups.append((f"{tab}!{_col_letter(cj)}{row1}", [[v]]))
    return ups


def _write_tab(service, tab, day_to_slice, layout, allow_new):
    from sync.sheets_write import batch_write

    formulas, name_to_col, _, row_of, next0 = layout
    updates = []
    written, missing = [], []
    for iso, slc in sorted(day_to_slice.items()):
        ri0 = row_of.get(iso)
        is_new = ri0 is None
        if is_new:
            if not allow_new:
                missing.append(iso)
                continue
            ri0 = next0
            next0 += 1
            row_of[iso] = ri0
        updates += _slice_cells(tab, iso, slc, name_to_col, formulas, ri0, is_new)
        written.append(iso)
    n = batch_write(service, SHEET_ID, updates)
    print(f"{tab}: {len(written)} дн., {n} ячеек" + (f"; нет строк: {missing}" if missing else ""))
    return n


def _run(service, dates, allow_new):
    conn = psycopg2.connect(os.environ["DATABASE_URL"].split("?")[0], connect_timeout=30)
    try:
        traffic, orders = fetch_web(conn, dates)
    finally:
        conn.close()
    for iso in dates:
        t, o = traffic[iso], orders[iso]
        print(f"{iso}: трафик org={t['org']['Total']} paid={t['paid']['Total']} | "
              f"заказы org={o['org']['Total']} paid={o['paid']['Total']}")
    _write_tab(service, TRAFFIC_TAB, traffic, _tab_layout(service, TRAFFIC_TAB), allow_new)
    _write_tab(service, ORDERS_TAB, orders, _tab_layout(service, ORDERS_TAB), allow_new)


def _dates(frm: date, to: date) -> list[str]:
    out, d = [], frm
    while d <= to:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def refresh(service) -> None:
    to = _msk_today() - timedelta(days=1)
    frm = to - timedelta(days=REFRESH_DAYS - 1)
    _run(service, _dates(frm, to), allow_new=True)


def build(service) -> None:
    to_env = os.environ.get("LIME_GCC_TO")
    to = date.fromisoformat(to_env) if to_env else _msk_today() - timedelta(days=1)
    _run(service, _dates(date.fromisoformat(BUILD_FROM), to), allow_new=True)


def probe(service) -> None:
    from sync.sheets_write import list_tabs, read_values

    print("Вкладки:", list_tabs(service, SHEET_ID))
    for tab in (TRAFFIC_TAB, ORDERS_TAB):
        try:
            vals = read_values(service, SHEET_ID, f"{tab}!A1:BZ8", render="FORMATTED_VALUE")
            print(f"\n=== {tab} (первые строки) ===")
            for i, row in enumerate(vals[:8]):
                print(f"  [{i}] {row}")
        except Exception as e:  # noqa: BLE001
            print(f"  !! {tab}: {type(e).__name__}: {e}")
    # сверка агрегата за вчера
    to = _msk_today() - timedelta(days=1)
    dates = _dates(to - timedelta(days=REFRESH_DAYS - 1), to)
    conn = psycopg2.connect(os.environ["DATABASE_URL"].split("?")[0], connect_timeout=30)
    try:
        traffic, orders = fetch_web(conn, dates)
    finally:
        conn.close()
    print("\n=== web-агрегат (сверка) ===")
    for iso in dates:
        t, o = traffic[iso], orders[iso]
        print(f"  {iso} трафик Total org/paid={t['org']['Total']}/{t['paid']['Total']} "
              f"UAE={t['org']['UAE']}/{t['paid']['UAE']} | заказы Total={o['org']['Total']}/{o['paid']['Total']}")


def main() -> None:
    from sync.sheets_write import get_write_service

    mode = os.environ.get("LIME_GCC_MODE") or "refresh"
    service = get_write_service()
    if mode == "probe":
        probe(service)
    elif mode == "refresh":
        refresh(service)
    elif mode == "build":
        build(service)
    else:
        raise SystemExit(f"lime_gcc_report: неизвестный режим {mode!r}")


if __name__ == "__main__":
    main()
