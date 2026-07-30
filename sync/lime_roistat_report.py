# -*- coding: utf-8 -*-
"""sync/lime_roistat_report.py — визиты и продажи Роистата (LIME) → Google-таблица.

Отдельная задача с собственным кроном (02:44 МСК — раньше 5:00 с запасом на задержку
планировщика GitHub), НЕ связана с lime_kz_roistat → Supabase: своя судьба, чинить в изоляции.

Считает по дню:
  • визиты платные / бесплатные / всего — free/paid берём из roistat_channels.map_*;
  • продажи (оплаченные заказы) и заявки по дате — из тех же дневных строк fetch_day.

Пишет в книгу LIME (вкладки Fact Traffic / Fact Orders) сопоставлением по ДАТЕ:
строки будущих дат в книге уже созданы, поэтому находим строку дня и обновляем
ячейки, а не добавляем новую.

Режимы (LIME_REPORT_MODE):
  refresh (default) — устойчивый инкремент: перезаписывает точечно последние
                      REFRESH_DAYS дней (default 7), историю не трогает. День без
                      данных Роистата НЕ затирается нулями — пропускается с ретраем.
  build             — строит вкладки с нуля и заливает окно [BUILD_FROM; вчера].
                      Для первичного бэкфилла.
  probe             — только читает и печатает структуру книги + сверку, не пишет.
  write             — дате-матч в готовую книгу заказчика (путь B, formula-safe).

Устойчивость: fetch_day уже ретраит транзиентные ошибки API; refresh сверху ретраит
пустой ответ (данные дня ещё не готовы) и переживает падение одного дня, не роняя
прогон целиком.

ENV: ROISTAT_API_KEY, ROISTAT_PROJECT_ID (default 235593),
     LIME_REPORTS_SA_JSON / GSC_SA_KEY (ключ сервис-аккаунта), LIME_REPORT_SHEET_ID,
     LIME_REPORT_REFRESH_DAYS (default 7), LIME_REPORT_FROM/LIME_REPORT_TO (бэкфилл build).
"""
from __future__ import annotations

import os
import re
import time
from datetime import date, datetime, timedelta, timezone

from sync.roistat_api import fetch_day
from sync.roistat_channels import PAID, map_roistat_channel

# Отчёт живёт в московских датах (крон 02:44 МСK = 23:44 UTC пред. дня — стартуем
# раньше, компенсируя задержку планировщика GitHub). Раннер в UTC, поэтому «вчера»
# считаем по Москве, иначе до полуночи UTC окно отставало бы на день. МСК=UTC+3 без DST.
def _msk_today() -> date:
    return (datetime.now(timezone.utc) + timedelta(hours=3)).date()

# Дата в книге: «Sun 05/07/2026» (слэши) ИЛИ «Сб 04.07.2026» (точки) — тянем DD/MM/YYYY.
# Заказчик сменил формат на слэши + англ. день недели; поддерживаем оба разделителя.
_DATE_RE = re.compile(r"(\d{2})[./](\d{2})[./](\d{4})")

# Заголовок колонки → ключ агрегата. «сайт» и «общий» = итог (органический+платный),
# пишем в оба; если какой-то из них формула — шаг записи его не тронет.
_TRAFFIC_TARGETS = {
    "Трафик органический": "free_visits",
    "Трафик платный": "paid_visits",
    "Трафик сайт": "total_visits",
    "Трафик общий": "total_visits",
}

_EN_WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Колонки вкладок при построении с нуля (режим build) — как на скрине заказчика.
_TRAFFIC_HEADER = ["Дата", "Трафик органический", "Трафик платный",
                   "Трафик сайт", "Трафик общий", "Год", "Месяц"]
_ORDERS_HEADER = ["Дата", "Продажи платный", "Продажи бесплатный",
                  "Продажи всего", "Год", "Месяц"]

# Старт истории для build (первичный бэкфилл). env LIME_REPORT_FROM переопределяет.
BUILD_FROM = os.environ.get("LIME_REPORT_FROM") or "2026-07-01"

# refresh перезаписывает последние N дней (данные Роистата дозревают: поздние заказы,
# доклейка визитов). 7 дней — комфортный запас, окно ограничено (не растёт бесконечно).
REFRESH_DAYS = int(os.environ.get("LIME_REPORT_REFRESH_DAYS") or "7")
# Пустой ответ Роистата за день (данные ещё не готовы) — ретраим отдельно от ошибок API.
EMPTY_RETRIES = int(os.environ.get("LIME_REPORT_EMPTY_RETRIES") or "3")
EMPTY_RETRY_SLEEP = int(os.environ.get("LIME_REPORT_EMPTY_RETRY_SLEEP") or "20")


def _date_label(iso: str) -> str:
    """ISO-дата → «Sun 05/07/2026» (англ. день + слэши) как в книге заказчика."""
    d = date.fromisoformat(iso)
    return f"{_EN_WD[d.weekday()]} {d.strftime('%d/%m/%Y')}"


def _traffic_row(iso: str, a: dict) -> list:
    dt = date.fromisoformat(iso)
    return [_date_label(iso), a["free_visits"], a["paid_visits"],
            a["total_visits"], a["total_visits"], dt.year, dt.month]


def _orders_row(iso: str, a: dict) -> list:
    dt = date.fromisoformat(iso)
    return [_date_label(iso), a["paid_orders"], a["free_orders"],
            a["total_orders"], dt.year, dt.month]


def _fetch_agg_guarded(iso: str, key: str) -> tuple[dict, bool]:
    """Агрегат за день с ретраем ПУСТОГО ответа и защитой от падения дня.

    fetch_day уже ретраит транзиентные ошибки API. Здесь добавляем два слоя:
      • ошибка Роистата после его ретраев (RuntimeError) — ловим, пробуем ещё;
      • валидный, но пустой день (данные не готовы) — ретраим до EMPTY_RETRIES.

    Returns:
        (agg, has_data). has_data=False → день писать НЕ нужно (сохранить прежнее).
    """
    agg = aggregate_day([])
    for attempt in range(1, EMPTY_RETRIES + 1):
        try:
            cand = aggregate_day(fetch_day(iso, PROJECT, key))
        except RuntimeError as e:
            print(f"refresh: {iso} — ошибка Роистата ({e}), попытка {attempt}/{EMPTY_RETRIES}")
            cand = None
        if cand and (cand["total_visits"] > 0 or cand["total_orders"] > 0):
            return cand, True
        if attempt < EMPTY_RETRIES:
            print(f"refresh: {iso} — пусто, ретрай {attempt}/{EMPTY_RETRIES}")
            time.sleep(EMPTY_RETRY_SLEEP * attempt)
    return agg, False

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
        to = _msk_today() - timedelta(days=1)
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

    rng = f"{TRAFFIC_TAB}!A1:Z3000"
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
    to = date.fromisoformat(to_env) if to_env else _msk_today() - timedelta(days=1)
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
    from sync.sheets_write import add_tab, list_tabs, read_values, write_block

    key = os.environ["ROISTAT_API_KEY"]
    aggs = {d: aggregate_day(fetch_day(d, PROJECT, key)) for d in dates}

    tabs = set(list_tabs(service, SHEET_ID))
    for tab in (TRAFFIC_TAB, ORDERS_TAB):
        if tab in tabs:
            # build пишет блок от A1; если во вкладке уже есть «Дата» (возможно
            # сдвинутая вставкой столбца) — перезапись от A1 всё разъедет. Тогда refresh.
            existing = read_values(service, SHEET_ID, f"{tab}!A1:Z3000", render="FORMATTED_VALUE")
            if _header_row(existing) is not None:
                raise RuntimeError(
                    f"{tab}: уже заполнена — build перезаписал бы от A1 и разъехал столбцы. "
                    f"Используй refresh (он пишет по именам колонок).")
        else:
            add_tab(service, SHEET_ID, tab)
            print(f"создана вкладка «{tab}»")

    traffic = [_TRAFFIC_HEADER]
    orders = [_ORDERS_HEADER]
    for iso in dates:
        a = aggs[iso]
        traffic.append(_traffic_row(iso, a))
        orders.append(_orders_row(iso, a))
        print(f"{iso}: визиты платн={a['paid_visits']} беспл={a['free_visits']} "
              f"всего={a['total_visits']} | продажи платн={a['paid_orders']} "
              f"беспл={a['free_orders']} всего={a['total_orders']}")

    write_block(service, SHEET_ID, f"{TRAFFIC_TAB}!A1", traffic)
    write_block(service, SHEET_ID, f"{ORDERS_TAB}!A1", orders)
    print(f"\nзаписано: {TRAFFIC_TAB} и {ORDERS_TAB} — {len(dates)} дн. ({dates[0]}…{dates[-1]})")


# Заголовок → ключ агрегата ДЛЯ КАЖДОЙ вкладки. Пишем по ИМЕНИ колонки, а не по
# фиксированной позиции: заказчик может вставить/переставить столбцы (так и вышло —
# перед «Дата» добавили пустой столбец, данные съехали в B:H).
_VALUE_MAP = {
    TRAFFIC_TAB: {
        "Трафик органический": "free_visits",
        "Трафик платный": "paid_visits",
        "Трафик сайт": "total_visits",
        "Трафик общий": "total_visits",
    },
    ORDERS_TAB: {
        # Новые имена заказчика; «Orders (APP Traffic)» не заполняем — у Роистата KZ
        # приложения нет, колонка остаётся пустой/ручной.
        "Orders (Paid Traffic)": "paid_orders",
        "Orders (Organic Traffic)": "free_orders",
        "Orders (Org + Paid)": "total_orders",
        # Старые имена — на случай отката формата.
        "Продажи платный": "paid_orders",
        "Продажи бесплатный": "free_orders",
        "Продажи всего": "total_orders",
    },
}


def _tab_layout(service, tab: str):
    """(formulas, name_to_col, date_col, row_of, next_row0) вкладки по ИМЕНАМ колонок.

    Читает широкий диапазон и находит строку заголовков + колонку «Дата» — устойчиво к
    вставке/перестановке столбцов. row_of: ISO-дата → 0-based индекс строки; next_row0 —
    0-based индекс первой свободной строки под данными (для дописи новой даты).
    """
    from sync.sheets_write import read_values

    rng = f"{tab}!A1:Z3000"
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


def _day_cell_updates(tab, iso, agg, name_to_col, formulas, ri0, is_new):
    """Ячейки-обновления одного дня по именам колонок. Формулы не трогаем (кроме новой строки)."""
    dt = date.fromisoformat(iso)
    row1 = ri0 + 1  # 1-based
    values = dict(_VALUE_MAP[tab])

    def is_formula(cj):
        return (ri0 < len(formulas) and cj < len(formulas[ri0])
                and str(formulas[ri0][cj]).startswith("="))

    ups = []
    for hdr, key in values.items():
        cj = name_to_col.get(hdr)
        if cj is None or (not is_new and is_formula(cj)):
            continue
        ups.append((f"{tab}!{_col_letter(cj)}{row1}", [[agg[key]]]))
    if is_new:  # новую строку заполняем целиком: Дата/Год/Месяц тоже
        for hdr, val in (("Дата", _date_label(iso)), ("Год", dt.year), ("Месяц", dt.month)):
            cj = name_to_col.get(hdr)
            if cj is not None:
                ups.append((f"{tab}!{_col_letter(cj)}{row1}", [[val]]))
    return ups


def refresh(service) -> None:
    """Устойчивый инкремент: точечно перезаписать последние REFRESH_DAYS дней.

    Пишем по ИМЕНАМ колонок (устойчиво к вставке столбцов). История вне окна не
    трогается; формулы не перезаписываем; день без данных Роистата пропускается
    (прежнее значение сохраняется), а не затирается нулями; новая дата дописывается.
    """
    from sync.sheets_write import batch_write, list_tabs

    key = os.environ["ROISTAT_API_KEY"]
    tabs = set(list_tabs(service, SHEET_ID))
    for tab in (TRAFFIC_TAB, ORDERS_TAB):
        if tab not in tabs:
            raise RuntimeError(f"вкладки «{tab}» нет — сначала прогони build для истории")

    layouts = {tab: list(_tab_layout(service, tab)) for tab in (TRAFFIC_TAB, ORDERS_TAB)}

    to = _msk_today() - timedelta(days=1)
    frm = to - timedelta(days=REFRESH_DAYS - 1)

    updates: list[tuple[str, list[list]]] = []
    written, skipped = [], []
    d = frm
    while d <= to:
        iso = d.isoformat()
        agg, has = _fetch_agg_guarded(iso, key)
        if not has:
            skipped.append(iso)
            d += timedelta(days=1)
            continue

        for tab in (TRAFFIC_TAB, ORDERS_TAB):
            formulas, name_to_col, _, row_of, next0 = layouts[tab]
            ri0 = row_of.get(iso)
            is_new = ri0 is None
            if is_new:
                ri0 = next0
                layouts[tab][4] = next0 + 1  # сдвинуть указатель свободной строки
                row_of[iso] = ri0
            updates += _day_cell_updates(tab, iso, agg, name_to_col, formulas, ri0, is_new)

        written.append(iso)
        print(f"{iso}: визиты платн={agg['paid_visits']} беспл={agg['free_visits']} "
              f"всего={agg['total_visits']} | продажи={agg['total_orders']}")
        d += timedelta(days=1)

    n = batch_write(service, SHEET_ID, updates)
    print(f"\nrefresh: окно {frm}…{to}, записано {len(written)} дн., {n} ячеек.")
    if skipped:
        print(f"refresh: без данных (сохранено прежнее): {skipped}")


def main() -> None:
    from sync.sheets_write import get_write_service

    mode = os.environ.get("LIME_REPORT_MODE") or "refresh"
    service = get_write_service()

    if mode == "probe":
        probe(service)
    elif mode == "refresh":
        refresh(service)
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
