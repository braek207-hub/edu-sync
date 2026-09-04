# -*- coding: utf-8 -*-
"""sync/lime_gcc_report.py — трафик и заказы GCC → Google-таблица (широкий формат).

Комбинация RU-формата (Web/App × Org/Paid) и GCC-разбивки по странам Залива. По каждому
срезу (весь GCC + 5 стран UAE/KSA/QA/KW/OM) семь колонок:
    Web Org · Web Paid · App Org · App Paid · WEB Total · APP Total · Total(«Общий» у GCC).

Источники:
  • WEB (трафик — GA4 417919368 c 2026-08-20, до того Яндекс.Метрика; заказы = purchases
    Triple Whale) — уже в lime_stats (region='gcc', data_source='web') с country + traffic_type.
  • APP (app 6299245, трафик = sessions, заказы = e-commerce) — ФАЗА 2, пока нули.
  • ЖИВАЯ Метрика 98232701 — витрина счётчиков lime_gcc_counter_daily (source='metrika'),
    единственный независимый от GA4 источник в книге; на нём стоят оба листа сверки.

«GCC»-срез = весь регион, включая country=NULL (визиты/заказы без страны), поэтому по
странам он НЕ сходится с суммой пяти — так в данных.

Вкладки: «Fact Traffic GCC» (трафик витрины) · «Fact Orders GCC» (заказы linear) ·
«Fact Orders LPC» · «Fact Traffic GA4» (web-часть витрины, тот же GA4) ·
«Fact Traffic Metrika» (живая Метрика) · «Сверка web DAU» · «Сверка CR web».

Режимы (LIME_GCC_MODE): build (создать вкладки + залить окно) | refresh (обновить
последние N дней, дописать новые даты) | probe (read-only). Пишем по ИМЕНАМ колонок.

ENV: DATABASE_URL, GSC_SA_KEY/GOOGLE_APPLICATION_CREDENTIALS (SA lime-reports),
     LIME_GCC_SHEET_ID, LIME_GCC_REFRESH_DAYS (7), LIME_GCC_FROM/LIME_GCC_TO (build).
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone

import psycopg2

SHEET_ID = os.environ.get("LIME_GCC_SHEET_ID") or "1JSM7wcZlNnKX6uB4kk7UwkQzv7QEgPyyYUiOD-77WiE"
TRAFFIC_TAB = os.environ.get("LIME_GCC_TRAFFIC_TAB") or "Fact Traffic GCC"
ORDERS_TAB = os.environ.get("LIME_GCC_ORDERS_TAB") or "Fact Orders GCC"
GA4_TAB = os.environ.get("LIME_GCC_GA4_TAB") or "Fact Traffic GA4"
# ЖИВАЯ Яндекс.Метрика 98232701 (lime_gcc_counter_daily, source='metrika') — независимый от
# GA4 счётчик. Своя вкладка нужна с 2026-08-20: после перевода витрины на GA4 «Fact Traffic
# GCC» перестал быть Метрикой, и сверка сравнивала GA4 сам с собой (шов на 13.08 = окно
# refresh(7д) в день переключения).
METRIKA_TAB = os.environ.get("LIME_GCC_METRIKA_TAB") or "Fact Traffic Metrika"
COMPARE_TAB = os.environ.get("LIME_GCC_COMPARE_TAB") or "Сверка web DAU"
# Заказы по НОВОЙ методике (весь заказ последней платной площадке, lastPlatformClick) —
# отдельный лист рядом с linear-листом, структура колонок та же.
ORDERS_LPC_TAB = os.environ.get("LIME_GCC_ORDERS_LPC_TAB") or "Fact Orders LPC"
# Сверка конверсий было/стало на формулах (web): числители из двух листов заказов,
# знаменатели из GA4-листа (было) и Метрика-листа (стало) — Метрика берётся из METRIKA_TAB,
# а не из витринной вкладки: витрина с 2026-08-20 сама на GA4.
CR_TAB = os.environ.get("LIME_GCC_CR_TAB") or "Сверка CR web"

REFRESH_DAYS = int(os.environ.get("LIME_GCC_REFRESH_DAYS") or "7")
BUILD_FROM = os.environ.get("LIME_GCC_FROM") or "2025-08-01"

# APP (AppMetrica app 6299245, LIME International — мультистрановое, фильтр по Заливу).
APP_ID = os.environ.get("GCC_APP_ID") or "6299245"
# Сессии за всю историю тянуть непрактично → app наливаем окном: refresh(7д) целиком,
# build — последние APP_BACKFILL дней. Web-история при этом полная.
APP_BACKFILL = int(os.environ.get("LIME_GCC_APP_BACKFILL_DAYS") or "30")
APP_LOOKBACK = int(os.environ.get("LIME_GCC_APP_LOOKBACK_DAYS") or "90")

# lime_stats хранит страну по-русски; книга — кодами Залива (без BH — Павел подтвердил).
_COUNTRY_CODE = {
    "ОАЭ": "UAE",
    "Саудовская Аравия": "KSA",
    "Катар": "QA",
    "Кувейт": "KW",
    "Оман": "OM",
}
_CODES = ("UAE", "KSA", "QA", "KW", "OM")
_GCC = "GCC"  # внутренний ключ среза «весь регион»
_SCOPES = (_GCC,) + _CODES

_EN_WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
# Дата: «Mon 08/09/2025» (слэши, англ.день) ИЛИ «Сб 04.07.2026» (точки) — оба разделителя.
_DATE_RE = re.compile(r"(\d{2})[./](\d{2})[./](\d{4})")

_META_COLS = ("Дата", "Год", "Месяц", "Неделя")
# Порядок метрик среза. grand — имя итоговой колонки: у GCC «Общий», у стран «{code} Total».
_METRIC_SUFFIX = ("Web Org", "Web Paid", "App Org", "App Paid", "WEB Total", "APP Total")


def _grand_col(scope: str) -> str:
    return "Общий" if scope == _GCC else f"{scope} Total"


def _scope_cols(scope: str) -> list[str]:
    """Имена 7 колонок среза в порядке шаблона."""
    prefix = "" if scope == _GCC else f"{scope} "
    return [f"{prefix}{s}" for s in _METRIC_SUFFIX] + [_grand_col(scope)]


def _header() -> list[str]:
    cols = list(_META_COLS)
    for scope in _SCOPES:
        cols += _scope_cols(scope)
    return cols


def _msk_today() -> date:
    """Московская дата (UTC+3, без DST) — окно по Москве, раннер в UTC."""
    return (datetime.now(timezone.utc) + timedelta(hours=3)).date()


def _date_label(iso: str) -> str:
    """ISO → «Mon 08/09/2025» (англ. день + слэши), как в KZ и в загруженной истории."""
    d = date.fromisoformat(iso)
    return f"{_EN_WD[d.weekday()]} {d.strftime('%d/%m/%Y')}"


# ─────────────────────────── агрегация ───────────────────────────

def _empty_metrics() -> dict:
    return {"web_org": 0, "web_paid": 0, "app_org": 0, "app_paid": 0}


def _empty_day() -> dict:
    return {scope: _empty_metrics() for scope in _SCOPES}


_STATS_SQL = """
SELECT date::text AS d, country, traffic_type,
       COALESCE(SUM(users), 0)::bigint AS users
FROM lime_stats
WHERE region = 'gcc' AND data_source = 'web' AND date = ANY(%s::date[])
GROUP BY date, country, traffic_type
"""


def fetch_web(conn, dates: list[str]) -> dict:
    """traffic: date → {scope → metrics}. web-трафик = DAU (ym:s:users), не визиты.

    Заказы здесь НЕ считаются (linearAll из TW+Shopify, fetch_orders_linear); app-трафик
    (DAU) доливает AppMetrica в _run. GCC-срез = весь регион (в т.ч. country=NULL).
    ⚠️ users по paid/organic не строго аддитивен: юзер, активный в обоих сегментах за день,
    попадёт в оба (Total чуть больше суммы уникальных) — как в Метрике по сегментам.
    """
    traffic = {d: _empty_day() for d in dates}
    with conn.cursor() as cur:
        cur.execute(_STATS_SQL, (dates,))
        for d, country, ttype, users in cur.fetchall():
            field = "web_paid" if ttype == "Платный" else "web_org"
            code = _COUNTRY_CODE.get((country or "").strip())
            traffic[d][_GCC][field] += int(users)
            if code:
                traffic[d][code][field] += int(users)
    return traffic


_METRIKA_SQL = """
SELECT date::text AS d, country, traffic_type,
       COALESCE(SUM(users), 0)::bigint AS users
FROM lime_gcc_counter_daily
WHERE source = 'metrika' AND date = ANY(%s::date[])
GROUP BY date, country, traffic_type
"""


def fetch_metrika_web(conn, dates: list[str]) -> dict:
    """ЖИВАЯ Метрика 98232701: date → {scope → {org, paid}} по web DAU (ym:s:users).

    Источник — витрина счётчиков lime_gcc_counter_daily (sync/lime_gcc_counters.py), не
    lime_stats: витрина GCC с 2026-08-20 наполняется GA4. Пайплайн Метрики добирает крест
    Stat API остатком ВНУТРИ страны, поэтому сумма каналов страны = чистый тотал страны,
    а GCC = сумма 5 стран (строк без страны там нет).
    """
    out = {d: {s: {"org": 0, "paid": 0} for s in (_GCC,) + _CODES} for d in dates}
    with conn.cursor() as cur:
        cur.execute(_METRIKA_SQL, (dates,))
        for d, country, ttype, users in cur.fetchall():
            if d not in out:
                continue
            field = "paid" if ttype == "Платный" else "org"
            code = _COUNTRY_CODE.get((country or "").strip())
            if not code:
                continue
            out[d][code][field] += int(users)
            out[d][_GCC][field] += int(users)
    return out


def _linear_paid_frac(order: dict) -> float:
    """Доля заказа, отнесённая к ПЛАТНОМУ трафику по linearAll (1/N на касание).

    Нет касаний → 0 (органика). Классификация касания paid/organic — map_tw_source.
    """
    from sync.gcc_triplewhale import map_tw_source
    la = (order.get("attribution") or {}).get("linearAll") or []
    if not la:
        return 0.0
    paid = sum(1 for tp in la
               if map_tw_source(tp.get("source"), (tp.get("campaignId") or "").strip() or None)[2] == "Платный")
    return paid / len(la)


def _order_paid_lpc(order: dict) -> bool:
    """Платный ли заказ по НОВОЙ методике: последняя платная площадка пути.

    order_touchpoint отдаёт первый непустой тачпоинт lastPlatformClick→lastClick→fullLastClick;
    классификация — тем же map_tw_source, что и у linear (одна таксономия каналов).
    Нет атрибуции → бесплатный.
    """
    from sync.gcc_triplewhale import map_tw_source, order_touchpoint
    tp = order_touchpoint(order)
    if not tp:
        return False
    referrer = (tp.get("campaignId") or "").strip() or None
    return map_tw_source(tp.get("source"), referrer)[2] == "Платный"


def aggregate_orders_lpc(orders_by_date: dict, country_map: dict, app_ids: set,
                         dates: list[str]) -> dict:
    """Заказы по lastPlatformClick (весь заказ платной площадке): date → {scope → metrics}.

    В отличие от linear здесь нет дробей: каждый заказ целиком либо paid, либо organic —
    web_paid+web_org = число web-заказов среза, тотал совпадает с linear-листом по построению.
    """
    out = {d: _empty_day() for d in dates}
    for iso in dates:
        for o in orders_by_date.get(iso, []):
            oid = str(o.get("order_id") or "")
            plat = "app" if oid in app_ids else "web"
            code = _COUNTRY_CODE.get((country_map.get(oid) or "").strip())
            field = f"{plat}_paid" if _order_paid_lpc(o) else f"{plat}_org"
            for scope in (_GCC,) + ((code,) if code else ()):
                out[iso][scope][field] += 1
    return out


def aggregate_orders_linear(orders_by_date: dict, country_map: dict, app_ids: set,
                            dates: list[str]) -> dict:
    """Заказы по linearAll, округлённые с ТОЧНЫМ тоталом: date → {scope → metrics}.

    На срез×платформу: paid = round(сумма долей paid), organic = (число заказов − paid).
    Значит web_org+web_paid = число web-заказов среза, app_org+app_paid = число app —
    тотал всегда сходится 1-в-1, теряется только точность дележа paid/organic.
    """
    # (date, scope, platform) → [paid_frac, n]
    acc = {d: {s: {"web": [0.0, 0], "app": [0.0, 0]} for s in _SCOPES} for d in dates}
    for iso in dates:
        for o in orders_by_date.get(iso, []):
            oid = str(o.get("order_id") or "")
            plat = "app" if oid in app_ids else "web"
            code = _COUNTRY_CODE.get((country_map.get(oid) or "").strip())
            pf = _linear_paid_frac(o)
            for scope in (_GCC,) + ((code,) if code else ()):
                acc[iso][scope][plat][0] += pf
                acc[iso][scope][plat][1] += 1

    out = {d: _empty_day() for d in dates}
    for iso in dates:
        for scope in _SCOPES:
            for plat in ("web", "app"):
                paid_frac, n = acc[iso][scope][plat]
                paid = min(round(paid_frac), n)
                out[iso][scope][f"{plat}_paid"] = paid
                out[iso][scope][f"{plat}_org"] = n - paid
    return out


def _fetch_orders_raw(dates: list[str]) -> tuple[dict, dict, set] | None:
    """Сырьё заказов за окно: (orders_by_date, country_map, app_ids) из TW+Shopify.

    Один тяжёлый фетч (TW с journeys по дням) — на него садятся ОБЕ агрегации
    (linear и lastPlatformClick), чтобы листы считались из одного среза данных.
    Нет ключа TW → None (заказы 0).
    """
    from sync.gcc_shopify import fetch_order_meta
    from sync.gcc_triplewhale import fetch_tw_orders

    if not os.environ.get("GCC_TRIPLEWHALE_API_KEY"):
        print("orders: GCC_TRIPLEWHALE_API_KEY не задан — заказы 0")
        return None
    tw_key = os.environ["GCC_TRIPLEWHALE_API_KEY"]
    shop = os.environ["GCC_TW_SHOP_DOMAIN"]
    country_map, app_ids = fetch_order_meta(os.environ["API_LIME_SHOPIFY"], dates[0], dates[-1])
    by_date = {iso: fetch_tw_orders(tw_key, shop, iso, iso) for iso in dates}
    return by_date, country_map, app_ids


def fetch_orders_linear(dates: list[str]) -> dict:
    """Заказы GCC по linearAll из TW+Shopify (канал web/app + страна доставки)."""
    raw = _fetch_orders_raw(dates)
    if raw is None:
        return {d: _empty_day() for d in dates}
    return aggregate_orders_linear(*raw, dates)


def _row_values(iso: str, day: dict) -> dict:
    """colname → значение для одной строки (метрики + производные + мета)."""
    dt = date.fromisoformat(iso)
    out: dict[str, object] = {
        "Дата": _date_label(iso), "Год": dt.year, "Месяц": dt.month,
        "Неделя": dt.isocalendar()[1],
    }
    for scope in _SCOPES:
        m = day[scope]
        web_total = m["web_org"] + m["web_paid"]
        app_total = m["app_org"] + m["app_paid"]
        prefix = "" if scope == _GCC else f"{scope} "
        out[f"{prefix}Web Org"] = m["web_org"]
        out[f"{prefix}Web Paid"] = m["web_paid"]
        out[f"{prefix}App Org"] = m["app_org"]
        out[f"{prefix}App Paid"] = m["app_paid"]
        out[f"{prefix}WEB Total"] = web_total
        out[f"{prefix}APP Total"] = app_total
        out[_grand_col(scope)] = web_total + app_total
    return out


# ─────────────────────────── запись ───────────────────────────

def _norm(cell) -> str:
    return re.sub(r"\s+", " ", str(cell or "").replace("\n", " ")).strip()


def _header_row(grid: list[list]) -> int | None:
    for i, row in enumerate(grid):
        if any(_norm(c) == "Дата" for c in row):
            return i
    return None


def _col_letter(idx0: int) -> str:
    s, n = "", idx0 + 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _merge_app(dst: dict, app: dict) -> None:
    """Долить app_org/app_paid из app-агрегата в дневные срезы (web уже заполнен)."""
    for iso, day in app.items():
        for scope, m in day.items():
            dst[iso][scope]["app_org"] += m["app_org"]
            dst[iso][scope]["app_paid"] += m["app_paid"]


# метрика day-dict → имя колонки листа (без префикса среза)
_FIELD_COL = {"web_org": "Web Org", "web_paid": "Web Paid",
              "app_org": "App Org", "app_paid": "App Paid"}


def _fill_from_sheet(service, traffic: dict, dates: list[str], fields: tuple) -> None:
    """Заполнить в `traffic` указанные метрики значениями из текущего листа трафика.

    Нужно чтобы _refresh_tab не обнулил колонки источника, который сейчас не собрался
    (app упал → сохраняем app с листа; app-only режим → сохраняем web с листа). Тем самым
    производные Total пересчитываются корректно (обновлённая часть + сохранённая часть).
    """
    from sync.sheets_write import read_values

    grid = read_values(service, SHEET_ID, f"{TRAFFIC_TAB}!A1:BZ4000", render="FORMATTED_VALUE")
    hdr_i = _header_row(grid)
    if hdr_i is None:
        return
    name_to_col = {_norm(c): j for j, c in enumerate(grid[hdr_i])}
    date_col = name_to_col.get("Дата")
    if date_col is None:
        return

    def num(ri: int, col: str) -> int:
        cj = name_to_col.get(col)
        if cj is None or ri >= len(grid) or cj >= len(grid[ri]):
            return 0
        v = str(grid[ri][cj]).replace(",", "").replace("\xa0", "").strip()
        try:
            return int(float(v))
        except ValueError:
            return 0

    dset = set(dates)
    for i in range(hdr_i + 1, len(grid)):
        m = _DATE_RE.search(grid[i][date_col]) if date_col < len(grid[i]) else None
        if not m:
            continue
        iso = f"{m[3]}-{m[2]}-{m[1]}"
        if iso not in dset or iso not in traffic:
            continue
        for scope in _SCOPES:
            prefix = "" if scope == _GCC else f"{scope} "
            for f in fields:
                traffic[iso][scope][f] = num(i, f"{prefix}{_FIELD_COL[f]}")


def _apply_app_total(traffic: dict, app_total: dict, dates: list[str]) -> None:
    """Положить app DAU total (Reporting) в срезы БЕЗ деления: весь total в app_org, paid=0.

    Это «полный день без разбивки» — гарантированное утреннее состояние. Logs-фаза ниже
    (best-effort) потом перепишет app_org/app_paid реальным сплитом. GCC-срез Reporting уже
    сумма 5 стран, поэтому кладём как есть.
    """
    for iso in dates:
        for scope, total in app_total.get(iso, {}).items():
            traffic[iso][scope]["app_org"] = total
            traffic[iso][scope]["app_paid"] = 0


def _split_app_by_logs(traffic: dict, app_total: dict, logs: dict, dates: list[str]) -> None:
    """Разложить app total (Reporting) на org/paid по ДОЛЕ из Logs last-touch.

    Total берём точный из Reporting (=UI), а долю paid — из Logs (org/paid данного среза).
    app_paid = round(total × paid_доля), app_org = total − paid. Тотал остаётся Reporting-точным,
    делится только пропорция. Нет данных Logs по срезу → остаётся как есть (весь organic).
    """
    for iso in dates:
        for scope, total in app_total.get(iso, {}).items():
            lm = logs.get(iso, {}).get(scope)
            if not lm:
                continue
            lt = lm["app_org"] + lm["app_paid"]
            if lt <= 0:
                continue
            paid = min(round(total * lm["app_paid"] / lt), total)
            traffic[iso][scope]["app_paid"] = paid
            traffic[iso][scope]["app_org"] = total - paid


def _run(service, dates: list[str], mode: str) -> None:
    conn = psycopg2.connect(os.environ["DATABASE_URL"].split("?")[0], connect_timeout=30)
    try:
        traffic = fetch_web(conn, dates)
    finally:
        conn.close()

    token = os.environ.get("APPMETRICA_TOKEN")
    # ── Фаза 1: app DAU TOTAL из Reporting API (синхронно, надёжно = полный день без разбивки).
    app_total = {}
    if token:
        try:
            from sync.gcc_app import fetch_app_dau_total
            app_total = fetch_app_dau_total(token, APP_ID, dates)
            _apply_app_total(traffic, app_total, dates)
        except Exception as e:  # noqa: BLE001
            print(f"gcc_app: Reporting total ПРОПУЩЕН: {type(e).__name__}: {e}")
            if mode != "build":  # Reporting лёг → не обнулять app, сохранить значения листа
                _fill_from_sheet(service, traffic, dates, ("app_org", "app_paid"))
    else:
        print("gcc_app: APPMETRICA_TOKEN не задан — app-трафик 0")

    raw = _fetch_orders_raw(dates)  # один TW-фетч → обе модели атрибуции
    if raw is None:
        orders = orders_lpc = {d: _empty_day() for d in dates}
    else:
        orders = aggregate_orders_linear(*raw, dates)      # старая методика (linear)
        orders_lpc = aggregate_orders_lpc(*raw, dates)     # новая (последняя платная)

    # ЗАПИСЬ A — полный день гарантированно: web(Метрика) + app total(Reporting, без разбивки) + заказы.
    for tab, data in ((TRAFFIC_TAB, traffic), (ORDERS_TAB, orders)):
        if mode == "build":
            _build_tab(service, tab, dates, data)
        else:
            _refresh_tab(service, tab, dates, data)
    # LPC-лист — best-effort: до первого orders-lpc-build вкладки нет, синк не рушим.
    try:
        _refresh_tab(service, ORDERS_LPC_TAB, dates, orders_lpc)
    except RuntimeError as e:
        print(f"{ORDERS_LPC_TAB}: пропуск ({e}) — создать разово режимом orders-lpc-build")
    for iso in dates:
        g = traffic[iso][_GCC]
        print(f"{iso}: web {g['web_org']}/{g['web_paid']} app total {g['app_org'] + g['app_paid']}")

    # ── Фаза 2: уточнить app-сплит paid/organic из Logs last-touch (BEST-EFFORT).
    # Logs упал/таймаут → фаза 1 (total без разбивки) уже записана, утренние данные целы.
    if token and app_total and mode != "build":
        try:
            from sync.gcc_app import fetch_app_traffic
            app_dates = dates if len(dates) <= APP_BACKFILL else dates[-APP_BACKFILL:]
            logs = fetch_app_traffic(token, APP_ID, app_dates, APP_LOOKBACK)
            _split_app_by_logs(traffic, app_total, logs, app_dates)
            _refresh_tab(service, TRAFFIC_TAB, app_dates, traffic)  # перезапись app-сплита
            print(f"gcc_app: app-сплит из Logs записан за {len(app_dates)} дн.")
        except Exception as e:  # noqa: BLE001
            print(f"gcc_app: Logs-сплит ПРОПУЩЕН (app total без разбивки уже записан): "
                  f"{type(e).__name__}: {e}")


def _build_tab(service, tab: str, dates: list[str], data: dict,
               header: list | None = None, row_fn=None) -> None:
    """Создать вкладку (если нет) и залить заголовок + все строки одним блоком.

    header/row_fn по умолчанию — формат Метрики (_header/_row_values); GA4 передаёт свои.
    """
    from sync.sheets_write import add_tab, list_tabs, read_values, write_block

    header = header or _header()
    row_fn = row_fn or _row_values
    tabs = set(list_tabs(service, SHEET_ID))
    if tab not in tabs:
        add_tab(service, SHEET_ID, tab)
        print(f"создана вкладка «{tab}»")
    else:
        existing = read_values(service, SHEET_ID, f"{tab}!A1:BZ5", render="FORMATTED_VALUE")
        if _header_row(existing) is not None:
            raise RuntimeError(f"{tab}: уже заполнена — build перезапишет от A1. Используй refresh.")

    block = [header]
    for iso in dates:
        rv = row_fn(iso, data[iso])
        block.append([rv.get(c, "") for c in header])
    write_block(service, SHEET_ID, f"{tab}!A1", block)
    print(f"{tab}: залито {len(dates)} дн., {len(header)} колонок")


def _refresh_tab(service, tab: str, dates: list[str], data: dict, row_fn=None) -> None:
    """Обновить строки последних дней по дате-матчу; новые даты дописать. По ИМЕНАМ колонок.

    row_fn(iso, day) → {colname: value}; по умолчанию формат Метрики (_row_values), GA4 — свой.
    """
    from sync.sheets_write import batch_write, read_values

    row_fn = row_fn or _row_values

    rng = f"{tab}!A1:BZ4000"
    grid = read_values(service, SHEET_ID, rng, render="FORMATTED_VALUE")
    formulas = read_values(service, SHEET_ID, rng, render="FORMULA")
    hdr_i = _header_row(grid)
    if hdr_i is None:
        raise RuntimeError(f"{tab}: нет строки заголовков с «Дата» — сначала build")
    name_to_col = {_norm(c): j for j, c in enumerate(grid[hdr_i])}
    date_col = name_to_col["Дата"]

    row_of: dict[str, int] = {}
    next0 = hdr_i + 1
    for i in range(hdr_i + 1, len(grid)):
        m = _DATE_RE.search(grid[i][date_col] if date_col < len(grid[i]) else "")
        if m:
            row_of[f"{m[3]}-{m[2]}-{m[1]}"] = i
            next0 = i + 1

    def is_formula(ri, cj):
        return (ri < len(formulas) and cj < len(formulas[ri])
                and str(formulas[ri][cj]).startswith("="))

    # Даты старее последней в листе НЕ дописываем в конец (иначе строки уезжают не по порядку —
    # так случилось при refresh с широким окном). Их место — build/бэкфилл, не refresh.
    existing_max = max(row_of) if row_of else None

    updates, written, skipped_old = [], [], 0
    for iso in sorted(dates):
        ri = row_of.get(iso)
        is_new = ri is None
        if is_new and existing_max and iso < existing_max:
            skipped_old += 1
            continue
        if is_new:
            ri = next0
            next0 += 1
            row_of[iso] = ri
        rv = row_fn(iso, data[iso])
        for col, val in rv.items():
            cj = name_to_col.get(col)
            if cj is None:
                continue
            if col in ("Дата", "Год", "Месяц", "Неделя") and not is_new:
                continue  # мету на существующей строке не трогаем
            if not is_new and is_formula(ri, cj):
                continue  # формулы (напр. Total) не перезаписываем
            updates.append((f"{tab}!{_col_letter(cj)}{ri + 1}", [[val]]))
        written.append(iso)
    n = batch_write(service, SHEET_ID, updates)
    extra = f", пропущено старых дат {skipped_old}" if skipped_old else ""
    print(f"{tab}: refresh {len(written)} дн., {n} ячеек{extra}")


def _dates(frm: date, to: date) -> list[str]:
    out, d = [], frm
    while d <= to:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def probe(service) -> None:
    from sync.sheets_write import list_tabs, read_values

    print("Вкладки:", list_tabs(service, SHEET_ID))
    print("Ожидаемый заголовок (%d колонок):" % len(_header()))
    print("  " + " | ".join(_header()))
    from collections import Counter
    for tab in (TRAFFIC_TAB, ORDERS_TAB):
        try:
            grid = read_values(service, SHEET_ID, f"{tab}!A1:E4000", render="FORMATTED_VALUE")
            hdr_i = _header_row(grid)
            dc = next(j for j, c in enumerate(grid[hdr_i]) if _norm(c) == "Дата")
            isos = []
            for row in grid[hdr_i + 1:]:
                m = _DATE_RE.search(row[dc]) if dc < len(row) else None
                if m:
                    isos.append(f"{m[3]}-{m[2]}-{m[1]}")
            dups = [d for d, n in Counter(isos).items() if n > 1]
            print(f"\n=== {tab}: строк-дат {len(isos)}, уникальных {len(set(isos))} ===")
            print(f"  диапазон: {min(isos)} … {max(isos)}")
            print(f"  ДУБЛИ дат: {dups if dups else 'нет'}")
        except Exception as e:  # noqa: BLE001
            print(f"  !! {tab}: {type(e).__name__}: {e}")


def shopify_probe() -> None:
    """Проверка Shopify-токена и страны доставки. Ничего не пишет."""
    from collections import Counter

    from sync.gcc_shopify import SHOP, fetch_order_countries, fetch_order_sources

    frm, to = "2026-07-24", "2026-07-28"
    tok = os.environ["API_LIME_SHOPIFY"]
    m = fetch_order_countries(tok, frm, to)
    print(f"shopify: {SHOP}, заказов {len(m)} за {frm}..{to}")
    print("страны доставки:", Counter(v or "— вне Залива/нет адреса —" for v in m.values()).most_common())
    ex = "7091092619586"
    print(f"пример order {ex} → страна доставки: {m.get(ex, 'НЕТ в окне')}")
    # Двойной счёт app? Тегируется ли канал заказа (sourceName / app)
    src = fetch_order_sources(tok, frm, to)
    print("sourceName:", Counter((r["source"] or "∅") for r in src).most_common())
    print("app:", Counter((r["app"] or "∅") for r in src).most_common())


def metrika_probe() -> None:
    """Сравнить ym:s:regionCountry (гео посетителя) с ym:s:startURLDomain (витрина). Read-only."""
    import requests

    token = os.environ["GCC_METRICA_TOKEN"]
    counter = os.environ.get("GCC_METRICA_COUNTER_ID") or "98232701"
    for dim in ("ym:s:regionCountry", "ym:s:startURLDomain"):
        r = requests.get(
            "https://api-metrika.yandex.net/stat/v1/data",
            headers={"Authorization": f"OAuth {token}"},
            params={"ids": counter, "date1": "2026-07-20", "date2": "2026-07-26",
                    "metrics": "ym:s:visits", "dimensions": dim, "accuracy": "full", "limit": 40},
            timeout=60,
        )
        data = r.json()
        rows = data.get("data") or []
        print(f"\n=== {dim} (HTTP {r.status_code}, {len(rows)} строк) ===")
        for item in rows[:25]:
            d0 = item["dimensions"][0]
            print(f"  {d0.get('name')!r} (id={d0.get('id')}): {int(item['metrics'][0])}")


def app_orders_check() -> None:
    """Сверить app-заказы: Shopify app-канал vs AppMetrica, по странам. Read-only."""
    from collections import Counter

    from sync.gcc_app import _GCC, ISO_CODE, fetch_app
    from sync.gcc_shopify import fetch_order_meta, fetch_order_sources

    frm, to = "2026-07-19", "2026-07-28"
    country, app_ids = fetch_order_meta(os.environ["API_LIME_SHOPIFY"], frm, to)
    shop = Counter()
    for oid in app_ids:
        shop[_COUNTRY_CODE.get((country.get(oid) or "").strip(), "прочее")] += 1
    print(f"Shopify app-канал: {sum(shop.values())} заказов за {frm}..{to}")

    # что это за заказы: распределение app/channel + примеры номеров для проверки в Shopify
    src = fetch_order_sources(os.environ["API_LIME_SHOPIFY"], frm, to)
    print("app:", Counter((r["app"] or "∅") for r in src).most_common())
    print("channel:", Counter((r["channel"] or "∅") for r in src).most_common())
    print("sourceName:", Counter((r["source"] or "∅") for r in src).most_common())
    print("\nпримеры app-заказов (открой в Shopify):")
    for r in [x for x in src if x["app"] != "Online Store"][:12]:
        print(f"  {r['name']} | {r['created']} | {r['country']} | app={r['app']} | channel={r['channel']}")

    dates = _dates(date.fromisoformat(frm), date.fromisoformat(to))
    _, orders = fetch_app(os.environ["APPMETRICA_TOKEN"], APP_ID, dates)
    am = Counter()
    for d in dates:
        for code in ISO_CODE.values():
            am[code] += orders[d][code]["app_org"] + orders[d][code]["app_paid"]
    am_total = sum(orders[d][_GCC]["app_org"] + orders[d][_GCC]["app_paid"] for d in dates)
    print(f"AppMetrica app-заказы (GCC): {am_total}")
    print(f"\nПО СТРАНАМ  {'Shopify':>8} {'AppMetrica':>11}")
    for code in list(ISO_CODE.values()) + ["прочее"]:
        print(f"{code:<10} {shop.get(code, 0):>8} {am.get(code, 0):>11}")

    # ПО ДНЯМ: Shopify app-канал (по createdAt) vs AppMetrica (GCC)
    shop_by_day = Counter(r["created"] for r in src if r["app"] != "Online Store")
    print(f"\nПО ДНЯМ     {'Shopify':>8} {'AppMetrica':>11}")
    for d in dates:
        am_d = sum(orders[d][c]["app_org"] + orders[d][c]["app_paid"] for c in ISO_CODE.values())
        print(f"{d}  {shop_by_day.get(d, 0):>8} {am_d:>11}")


def tw_shopify_check() -> None:
    """Сходятся ли заказы TW и Shopify по order_id, и есть ли app-заказы в TW. Read-only."""
    from sync.gcc_shopify import fetch_order_sources
    from sync.gcc_triplewhale import fetch_tw_orders

    frm, to = "2026-07-19", "2026-07-28"
    tw = fetch_tw_orders(os.environ["GCC_TRIPLEWHALE_API_KEY"], os.environ["GCC_TW_SHOP_DOMAIN"], frm, to)
    tw_ids = {str(o.get("order_id") or "") for o in tw}
    src = fetch_order_sources(os.environ["API_LIME_SHOPIFY"], frm, to)
    shop_ids = {r["order_id"] for r in src}
    app_ids = {r["order_id"] for r in src if r["app"] != "Online Store"}
    web_ids = {r["order_id"] for r in src if r["app"] == "Online Store"}
    print(f"TW заказов: {len(tw_ids)}")
    print(f"Shopify заказов: {len(shop_ids)} (web {len(web_ids)}, app {len(app_ids)})")
    print(f"app-заказов Shopify, попавших в TW: {len(app_ids & tw_ids)} из {len(app_ids)}")
    print(f"web-заказов Shopify в TW: {len(web_ids & tw_ids)} из {len(web_ids)}")
    print(f"в TW, но нет в Shopify: {len(tw_ids - shop_ids)}")
    print(f"в Shopify, но нет в TW: {len(shop_ids - tw_ids)}")

    # Указан ли источник у app-заказов в TW? Распределение source + paid/organic.
    from collections import Counter

    from sync.gcc_triplewhale import map_tw_source, order_touchpoint
    tw_by_id = {str(o.get("order_id") or ""): o for o in tw}

    def dist(ids):
        src, pt = Counter(), Counter()
        for oid in ids:
            o = tw_by_id.get(oid)
            if not o:
                continue
            tp = order_touchpoint(o)
            s = tp.get("source")
            src[s or "∅(нет источника)"] += 1
            _, _, tt = map_tw_source(s, (tp.get("campaignId") or "").strip() or None)
            pt[tt] += 1
        return src, pt

    a_src, a_pt = dist(app_ids)
    w_src, w_pt = dist(web_ids)
    print(f"\nAPP-заказы (90) источник TW: {a_src.most_common()}")
    print(f"APP-заказы paid/organic: {a_pt.most_common()}")
    print(f"\nWEB-заказы источник TW: {w_src.most_common()[:8]}")
    print(f"WEB-заказы paid/organic: {w_pt.most_common()}")


def tw_order_dump() -> None:
    """Полная структура живого заказа TW (app vs web) — есть ли поле канала. Read-only."""
    import json

    from sync.gcc_shopify import fetch_order_meta
    from sync.gcc_triplewhale import fetch_tw_orders

    frm, to = "2026-07-19", "2026-07-28"
    tw = fetch_tw_orders(os.environ["GCC_TRIPLEWHALE_API_KEY"], os.environ["GCC_TW_SHOP_DOMAIN"], frm, to)
    _, app_ids = fetch_order_meta(os.environ["API_LIME_SHOPIFY"], frm, to)
    by_id = {str(o.get("order_id") or ""): o for o in tw}
    app_ex = next((by_id[i] for i in app_ids if i in by_id), None)
    web_ex = next((o for oid, o in by_id.items() if oid not in app_ids), None)

    def show(o, label):
        print(f"\n=== {label} order {o.get('order_id')} ===")
        for k, v in o.items():
            if k == "journey":
                j = v or []
                keys = list(j[0].keys()) if j else []
                print(f"  journey: {len(j)} тачпоинтов; keys тачпоинта: {keys}")
                if j:
                    print(f"    первый тачпоинт: {json.dumps(j[0], ensure_ascii=False)[:400]}")
            elif k == "attribution":
                print(f"  attribution keys: {list((v or {}).keys())}")
            else:
                print(f"  {k}: {json.dumps(v, ensure_ascii=False)[:200]}")

    if app_ex:
        show(app_ex, "APP")
    if web_ex:
        show(web_ex, "WEB")


def attr_compare() -> None:
    """Meta-заказы: lastPlatformClick (весь заказ) vs linearAll (дробно). Read-only.

    Сначала дампим attribution одного Meta-заказа (понять формат весов linearAll),
    потом считаем сумму по обеим моделям за окно.
    """
    import json

    from sync.gcc_triplewhale import fetch_tw_orders
    frm, to = "2026-07-19", "2026-07-28"
    tw = fetch_tw_orders(os.environ["GCC_TRIPLEWHALE_API_KEY"], os.environ["GCC_TW_SHOP_DOMAIN"], frm, to)

    def src(tp):
        return (tp or {}).get("source")

    ex = None
    for o in tw:
        lpc = (o.get("attribution") or {}).get("lastPlatformClick") or []
        if lpc and src(lpc[0]) == "facebook-ads":
            ex = o
            break
    if ex:
        a = ex.get("attribution") or {}
        print("=== пример Meta-заказа attribution ===")
        print("lastPlatformClick:", json.dumps(a.get("lastPlatformClick"), ensure_ascii=False)[:400])
        print("linearAll:", json.dumps(a.get("linearAll"), ensure_ascii=False)[:800])

    # lastPlatformClick: весь заказ источнику [0]. linearAll: 1/N на касание (поля веса нет).
    from collections import defaultdict
    lpc_m: dict[str, float] = defaultdict(float)   # lastPlatformClick (наша)
    lc_m: dict[str, float] = defaultdict(float)    # lastClick (любой последний)
    lin_m: dict[str, float] = defaultdict(float)   # linearAll (1/N)
    total = 0
    for o in tw:
        total += 1
        a = o.get("attribution") or {}
        lpc = a.get("lastPlatformClick") or []
        if lpc:
            lpc_m[src(lpc[0]) or "∅(нет)"] += 1
        lc = a.get("lastClick") or []
        if lc:
            lc_m[src(lc[0]) or "∅(нет)"] += 1
        la = a.get("linearAll") or []
        for tp in la:
            lin_m[src(tp) or "∅(нет)"] += 1 / len(la)
    print(f"\nвсего заказов: {total}")
    print(f"{'источник':<22}{'lastPlatform(наша)':>19}{'lastClick':>11}{'linearAll':>11}")
    for s in sorted(set(lpc_m) | set(lc_m) | set(lin_m), key=lambda k: -lpc_m.get(k, 0)):
        print(f"{s:<22}{lpc_m.get(s, 0):>19.0f}{lc_m.get(s, 0):>11.0f}{lin_m.get(s, 0):>11.1f}")


_CSV_TRAFFIC = os.environ.get("LIME_GCC_TRAFFIC_CSV") or "data/gcc_history/traffic.csv"
_CSV_ORDERS = os.environ.get("LIME_GCC_ORDERS_CSV") or "data/gcc_history/orders.csv"


def _parse_history_csv(path: str) -> dict:
    """CSV заказчика (ORG/PAID/Total по странам) → {iso: day-dict}. app=0 (всё web)."""
    import csv
    import io

    rows = list(csv.reader(io.StringIO(open(path, "rb").read().decode("utf-8-sig"))))
    hi = next(i for i, r in enumerate(rows) if any("ORG Total" in c for c in r))
    hdr = [c.strip() for c in rows[hi]]

    def col(name):
        return hdr.index(name) if name in hdr else None

    def num(v):
        v = str(v or "").replace(",", "").replace("\xa0", "").strip()
        try:
            return int(float(v))
        except ValueError:
            return 0

    org_t, paid_t, tot = col("ORG Total"), col("PAID Total"), col("Total")
    out: dict[str, dict] = {}
    for r in rows[hi + 1:]:
        if not r:
            continue
        m = _DATE_RE.search(r[0])
        if not m:
            continue
        # пустые будущие строки (нет данных) — пропускаем
        if num(r[org_t] if org_t is not None else 0) == 0 and num(r[paid_t] if paid_t is not None else 0) == 0:
            continue
        iso = f"{m[3]}-{m[2]}-{m[1]}"
        day = {s: {"web_org": 0, "web_paid": 0, "app_org": 0, "app_paid": 0} for s in _SCOPES}
        day[_GCC]["web_org"] = num(r[org_t])
        day[_GCC]["web_paid"] = num(r[paid_t])
        for code in _CODES:
            co, cp = col(f"ORG {code}"), col(f"PAID {code}")
            if co is not None and co < len(r):
                day[code]["web_org"] = num(r[co])
            if cp is not None and cp < len(r):
                day[code]["web_paid"] = num(r[cp])
        out[iso] = day
    return out


def load_history(service) -> None:
    """РАЗОВЫЙ режим: очистить вкладки GCC и залить историю из CSV (ORG→Web Org, app=0)."""
    from sync.sheets_write import clear_tab, write_block

    for tab, path in ((TRAFFIC_TAB, _CSV_TRAFFIC), (ORDERS_TAB, _CSV_ORDERS)):
        data = _parse_history_csv(path)
        dates = sorted(data)
        header = _header()
        block = [header] + [[_row_values(iso, data[iso]).get(c, "") for c in header] for iso in dates]
        clear_tab(service, SHEET_ID, f"{tab}!A1:BZ4000")
        write_block(service, SHEET_ID, f"{tab}!A1", block)
        print(f"{tab}: очищено + залито {len(dates)} дн. ({dates[0]}…{dates[-1]})")


def _write_traffic_partial(service, dates: list[str], data: dict, owns: str) -> None:
    """Записать трафик-лист, владея ТОЛЬКО одной стороной (owns='web'|'app').

    Другую сторону подтягиваем с листа (_fill_from_sheet), чтобы _refresh_tab её НЕ обнулил.
    ЕДИНСТВЕННАЯ точка частичной записи трафика — иначе легко забыть preserve (обнулит app/web)
    или задвоить через _merge_app (инцидент 2026-08-04: backfill обнулил app). `data` содержит
    свежими только колонки `owns`; противоположные должны быть 0 (не смерженными) на входе.
    """
    keep = ("app_org", "app_paid") if owns == "web" else ("web_org", "web_paid")
    _fill_from_sheet(service, data, dates, keep)
    _refresh_tab(service, TRAFFIC_TAB, dates, data)


def traffic_backfill(service) -> None:
    """РАЗОВЫЙ режим: заменить ВЕСЬ трафик листа на Метрику (web DAU) + AppMetrica (app DAU).

    Лист трафика раньше был залит из GA4 — переводим на наш Метрика-пайплайн за весь период.
    Заказы НЕ трогаем (лист «Fact Orders GCC» остаётся как есть, TW не дёргаем).
    Диапазон: LIME_GCC_FROM..вчера(МСК); FROM по умолчанию — мин. дата текущего листа трафика.
    Пишем через refresh (по дате-матчу): существующие даты перезаписываются Метрикой,
    отсутствующие дописываются. Формулы (Total) не трогаем.
    """
    from sync.sheets_write import read_values

    frm_env = os.environ.get("LIME_GCC_FROM")
    if frm_env:
        frm = date.fromisoformat(frm_env)
    else:
        grid = read_values(service, SHEET_ID, f"{TRAFFIC_TAB}!A1:E4000", render="FORMATTED_VALUE")
        hdr_i = _header_row(grid)
        dc = next(j for j, c in enumerate(grid[hdr_i]) if _norm(c) == "Дата")
        isos = [f"{m[3]}-{m[2]}-{m[1]}" for row in grid[hdr_i + 1:]
                if dc < len(row) and (m := _DATE_RE.search(row[dc]))]
        frm = date.fromisoformat(min(isos))
    to = _msk_today() - timedelta(days=1)
    dates = _dates(frm, to)

    conn = psycopg2.connect(os.environ["DATABASE_URL"].split("?")[0], connect_timeout=30)
    try:
        traffic = fetch_web(conn, dates)   # web DAU — весь период из lime_stats (app=0)
    finally:
        conn.close()

    # Web пишем СРАЗУ (главное — замена GA4 на Метрику). Владеем web, app сохраняем с листа
    # (Павел: «app не трогаем»). Гарантированно ложится, даже если AppMetrica ниже упрётся в таймаут.
    print(f"traffic-backfill web: {frm}…{to} ({len(dates)} дн.) — только web, app и заказы не тронуты")
    _write_traffic_partial(service, dates, traffic, "web")

    # App DAU — best-effort вторым проходом (свежий dict, владеем app, web сохраняем с листа).
    if os.environ.get("APPMETRICA_TOKEN"):
        app_dates = dates if len(dates) <= APP_BACKFILL else dates[-APP_BACKFILL:]
        try:
            from sync.gcc_app import fetch_app_traffic
            ta = fetch_app_traffic(os.environ["APPMETRICA_TOKEN"], APP_ID, app_dates, APP_LOOKBACK)
            appdata = {d: _empty_day() for d in app_dates}
            _merge_app(appdata, ta)
            _write_traffic_partial(service, app_dates, appdata, "app")
            print(f"traffic-backfill app: долит DAU за последние {len(app_dates)} дн.")
        except Exception as e:  # noqa: BLE001
            print(f"traffic-backfill app: ПРОПУЩЕН (web уже записан): {type(e).__name__}: {e}")
    else:
        print("gcc_app: APPMETRICA_TOKEN не задан — app-трафик 0")


def app_traffic_fill(service) -> None:
    """Добор app-трафика (DAU) в лист: собрать installs+deeplinks+sessions (без purchases) и
    записать ТОЛЬКО app-колонки (web сохраняется с листа). Для случая, когда web уже залит,
    а app нужно догнать. Окно: LIME_GCC_FROM..вчера(МСК), по умолчанию последние APP_BACKFILL дн.
    """
    from sync.gcc_app import fetch_app_traffic

    to_env = os.environ.get("LIME_GCC_TO")
    to = date.fromisoformat(to_env) if to_env else _msk_today() - timedelta(days=1)
    frm_env = os.environ.get("LIME_GCC_FROM")
    frm = date.fromisoformat(frm_env) if frm_env else to - timedelta(days=APP_BACKFILL - 1)
    dates = _dates(frm, to)

    ta = fetch_app_traffic(os.environ["APPMETRICA_TOKEN"], APP_ID, dates, APP_LOOKBACK)
    traffic = {d: _empty_day() for d in dates}
    _merge_app(traffic, ta)                                  # app заполнен, web=0
    for iso in dates:
        g = traffic[iso][_GCC]
        print(f"{iso}: app DAU {g['app_org']}/{g['app_paid']} (org/paid)")
    print(f"app-traffic-fill: {frm}…{to} ({len(dates)} дн.) — только app-колонки, web сохранён")
    _write_traffic_partial(service, dates, traffic, "app")  # владеем app, web сохраняем с листа


def verify(service) -> None:
    """Самопроверка: в обеих вкладках последняя дата = вчера(МСК). Иначе SystemExit(1)."""
    from sync.sheets_write import read_values
    expected = (_msk_today() - timedelta(days=1)).isoformat()
    stale = []
    for tab in (TRAFFIC_TAB, ORDERS_TAB):
        grid = read_values(service, SHEET_ID, f"{tab}!A1:E4000", render="FORMATTED_VALUE")
        hdr_i = _header_row(grid)
        dc = next(j for j, c in enumerate(grid[hdr_i]) if _norm(c) == "Дата")
        isos = [f"{m[3]}-{m[2]}-{m[1]}" for row in grid[hdr_i + 1:]
                if dc < len(row) and (m := _DATE_RE.search(row[dc]))]
        mx = max(isos) if isos else "нет"
        print(f"verify {tab}: последняя дата {mx}, ожидалась ≥ {expected}")
        if not isos or max(isos) < expected:
            stale.append(f"{tab}={mx}")
    if stale:
        raise SystemExit(f"verify: книга НЕ содержит вчерашней даты ({expected}): {stale} — синк не дошёл")
    print("verify: OK, книга актуальна")


def _ga4_header() -> list[str]:
    """Заголовок GA4-листа: мета + GCC(ORG/PAID/Total) + по 5 странам (ORG/PAID/Total)."""
    from sync.gcc_ga4 import GA4_CODES
    h = ["Дата", "Год", "Месяц", "Неделя", "ORG Total", "PAID Total", "Total"]
    for code in GA4_CODES:
        h += [f"ORG {code}", f"PAID {code}", f"Total {code}"]
    return h


def _ga4_row(iso: str, day: dict) -> dict:
    """colname → значение строки GA4. day = {scope: {org, paid}}; Total = org+paid."""
    from sync.gcc_ga4 import GA4_CODES
    dt = date.fromisoformat(iso)
    g = day["GCC"]
    out: dict[str, object] = {
        "Дата": _date_label(iso), "Год": dt.year, "Месяц": dt.month, "Неделя": dt.isocalendar()[1],
        "ORG Total": g["org"], "PAID Total": g["paid"], "Total": g["org"] + g["paid"],
    }
    for code in GA4_CODES:
        m = day[code]
        out[f"ORG {code}"] = m["org"]
        out[f"PAID {code}"] = m["paid"]
        out[f"Total {code}"] = m["org"] + m["paid"]
    return out


def ga4_compare_formulas(service) -> None:
    """Лист сверки на ФОРМУЛАХ (без синка): GA4 (витрина) vs ЖИВАЯ Метрика 98232701, web DAU.

    Тянет из готовых листов Fact Traffic GA4 и Fact Traffic Metrika по дате и имени колонки
    (ARRAYFORMULA+VLOOKUP+MATCH). Авто-обновляется, когда обновляются эти листы.
    Δ% = (GA4 − Метрика)/Метрика·100. По GCC и 5 странам.

    ⚠️ Метрика берётся из METRIKA_TAB, а НЕ из витринной «Fact Traffic GCC»: с 2026-08-20
    витрина сама на GA4, и до 2026-09-04 лист сверял GA4 сам с собой (шов на 13.08 —
    окно refresh(7д) в день переключения; ≤12.08 в витринной вкладке осталась Метрика).
    """
    from sync.gcc_ga4 import GA4_CODES
    from sync.sheets_write import add_tab, get_locale, list_tabs, write_formulas

    # Разделитель аргументов формул зависит от локали книги: comma-decimal (ru/eu) → ';', иначе ','.
    loc = get_locale(service, SHEET_ID)
    s = ";" if loc.split("_")[0] in ("ru", "de", "fr", "es", "it", "pt", "nl", "pl", "tr", "uk") else ","

    ga4, gcc = GA4_TAB, METRIKA_TAB  # 'Fact Traffic GA4', 'Fact Traffic Metrika'
    # (метка, имя «всего»-колонки; у обеих вкладок формат колонок одинаковый)
    scopes = [("GCC", "Total", "Total")] + [(c, f"Total {c}", f"Total {c}") for c in GA4_CODES]

    # A2 — даты, разлитые из GA4-листа (драйвер); диапазон растёт сам.
    header = ["Дата"]
    row2 = [f"=ARRAYFORMULA(IF('{ga4}'!$A$2:$A=\"\"{s}\"\"{s}'{ga4}'!$A$2:$A))"]
    for k, (label, ga4_h, metr_h) in enumerate(scopes):
        metr_L, ga4_L = _col_letter(1 + 3 * k), _col_letter(2 + 3 * k)
        header += [f"Метрика DAU {label}", f"GA4 всего {label}", f"Δ% {label}"]
        row2 += [
            (f"=ARRAYFORMULA(IF($A$2:$A=\"\"{s}\"\"{s}IFERROR(VLOOKUP($A$2:$A{s}'{gcc}'!$A:$BZ{s}"
             f"MATCH(\"{metr_h}\"{s}'{gcc}'!$1:$1{s}0){s}FALSE))))"),
            (f"=ARRAYFORMULA(IF($A$2:$A=\"\"{s}\"\"{s}IFERROR(VLOOKUP($A$2:$A{s}'{ga4}'!$A:$Z{s}"
             f"MATCH(\"{ga4_h}\"{s}'{ga4}'!$1:$1{s}0){s}FALSE))))"),
            # ДОЛЯ, не «×100»: столбцы Δ% в книге имеют процентный формат ячейки и сами
            # домножают на 100 при показе (иначе 21,1 рисуется как 2110,00%).
            (f"=ARRAYFORMULA(IF(${metr_L}$2:${metr_L}=\"\"{s}\"\"{s}IFERROR("
             f"({ga4_L}2:{ga4_L}-{metr_L}2:{metr_L})/{metr_L}2:{metr_L}{s}\"\")))"),
        ]

    print(f"локаль книги {loc}, разделитель '{s}'")
    if COMPARE_TAB not in set(list_tabs(service, SHEET_ID)):
        add_tab(service, SHEET_ID, COMPARE_TAB)
        print(f"создана вкладка «{COMPARE_TAB}»")
    write_formulas(service, SHEET_ID, f"{COMPARE_TAB}!A1", [header, row2])
    print(f"{COMPARE_TAB}: формулы записаны ({len(header)} колонок; авто-обновление из {ga4}/{gcc})")

    from sync.sheets_write import read_values
    chk = read_values(service, SHEET_ID, f"{COMPARE_TAB}!A1:E6", render="FORMATTED_VALUE")
    print("проверка (Дата | Метрика DAU GCC | GA4 всего GCC | Δ% GCC):")
    for r in chk:
        print("  ", [c for c in r[:5]])


def orders_lpc_build(service) -> None:
    """Разовый бэкфилл листа заказов по новой методике (lastPlatformClick) за всю историю.

    Окно: LIME_GCC_FROM (default BUILD_FROM) .. LIME_GCC_TO (default вчера МСК). Дальше лист
    ведёт ежедневный refresh в _run.
    """
    to_env = os.environ.get("LIME_GCC_TO")
    to = date.fromisoformat(to_env) if to_env else _msk_today() - timedelta(days=1)
    dates = _dates(date.fromisoformat(BUILD_FROM), to)
    print(f"orders-lpc-build: окно {dates[0]}..{dates[-1]} ({len(dates)} дн.)")
    raw = _fetch_orders_raw(dates)
    if raw is None:
        raise SystemExit("orders-lpc-build: нет GCC_TRIPLEWHALE_API_KEY")
    data = aggregate_orders_lpc(*raw, dates)
    _build_tab(service, ORDERS_LPC_TAB, dates, data)
    tot_paid = sum(data[d][_GCC]["web_paid"] + data[d][_GCC]["app_paid"] for d in dates)
    tot = sum(sum(data[d][_GCC][f] for f in ("web_paid", "web_org", "app_paid", "app_org"))
              for d in dates)
    print(f"orders-lpc-build: заказов {tot}, из них платных {tot_paid}")


def cr_compare_formulas(service) -> None:
    """Лист «Сверка CR web» на ФОРМУЛАХ: конверсия было/стало по дням, платный/бесплатный, страны.

    CR было = заказы linear ('Fact Orders GCC') / трафик GA4 ('Fact Traffic GA4');
    CR стало = заказы LPC ('Fact Orders LPC') / трафик Метрики ('Fact Traffic Metrika').
    Знаменатель «стало» — живая Метрика 98232701, а не витринная вкладка: та с 2026-08-20
    наполняется GA4, из-за чего «стало» считалось по тому же трафику, что и «было».
    Только web: у app источник трафика в обеих методиках один (AppMetrica), сравнивать нечего.
    Автообновление: формулы тянут значения из четырёх листов по дате и имени колонки.
    """
    from sync.sheets_write import add_tab, get_locale, list_tabs, read_values, write_formulas

    loc = get_locale(service, SHEET_ID)
    s = ";" if loc.split("_")[0] in ("ru", "de", "fr", "es", "it", "pt", "nl", "pl", "tr", "uk") else ","

    # У исторических листов шапка НЕ обязана быть в строке 1 (сверху примечания) — читаем
    # реальную строку заголовка и колонку «Дата» каждого листа, формулы строим по факту.
    meta: dict[str, dict] = {}
    for tab in (METRIKA_TAB, ORDERS_TAB, ORDERS_LPC_TAB, GA4_TAB):
        grid = read_values(service, SHEET_ID, f"{tab}!A1:BZ12", render="FORMATTED_VALUE")
        hdr_i = _header_row(grid)
        if hdr_i is None:
            raise SystemExit(f"cr-compare: в «{tab}» нет строки заголовков с «Дата»")
        cols = {_norm(c): j for j, c in enumerate(grid[hdr_i])}
        meta[tab] = {"row1": hdr_i + 2, "date_j": cols["Дата"], "cols": cols}
        print(f"{tab}: заголовок в строке {hdr_i + 1}, колонок {len(cols)}")

    def vlook(tab: str, col: str) -> str:
        m = meta[tab]
        cj = m["cols"].get(_norm(col))
        if cj is None:
            raise SystemExit(f"cr-compare: в «{tab}» нет колонки «{col}»")
        d_letter = _col_letter(m["date_j"])
        idx = cj - m["date_j"] + 1
        assert idx >= 1, (tab, col)
        return (f"VLOOKUP($A$2:$A{s}'{tab}'!${d_letter}${m['row1']}:$BZ{s}{idx}{s}FALSE)")

    def cr(num_tab: str, num_col: str, den_tab: str, den_col: str) -> str:
        return (f"=ARRAYFORMULA(IF($A$2:$A=\"\"{s}\"\"{s}IFERROR(ROUND("
                f"{vlook(num_tab, num_col)}/{vlook(den_tab, den_col)}*100{s}2){s}\"\")))")

    # (метка, префикс колонок листов заказов, ORG/PAID-колонки листов трафика —
    #  у GA4-вкладки и вкладки Метрики формат колонок одинаковый)
    scopes = [("GCC", "", "ORG Total", "PAID Total")] + \
             [(c, f"{c} ", f"ORG {c}", f"PAID {c}") for c in _CODES]

    t_m = meta[GA4_TAB]
    drv = f"'{GA4_TAB}'!${_col_letter(t_m['date_j'])}${t_m['row1']}:${_col_letter(t_m['date_j'])}"
    header = ["Дата"]
    row2 = [f"=ARRAYFORMULA(IF({drv}=\"\"{s}\"\"{s}{drv}))"]
    for label, p, ga_org, ga_paid in scopes:
        header += [f"{label} Paid CR было", f"{label} Paid CR стало",
                   f"{label} Org CR было", f"{label} Org CR стало"]
        row2 += [
            cr(ORDERS_TAB, f"{p}Web Paid", GA4_TAB, ga_paid),
            cr(ORDERS_LPC_TAB, f"{p}Web Paid", METRIKA_TAB, ga_paid),
            cr(ORDERS_TAB, f"{p}Web Org", GA4_TAB, ga_org),
            cr(ORDERS_LPC_TAB, f"{p}Web Org", METRIKA_TAB, ga_org),
        ]

    print(f"локаль книги {loc}, разделитель '{s}'")
    if CR_TAB not in set(list_tabs(service, SHEET_ID)):
        add_tab(service, SHEET_ID, CR_TAB)
        print(f"создана вкладка «{CR_TAB}»")
    write_formulas(service, SHEET_ID, f"{CR_TAB}!A1", [header, row2])
    print(f"{CR_TAB}: формулы записаны ({len(header)} колонок)")
    chk = read_values(service, SHEET_ID, f"{CR_TAB}!A1:E6", render="FORMATTED_VALUE")
    print("проверка (Дата | GCC Paid CR было | GCC Paid CR стало | GCC Org CR было | GCC Org CR стало):")
    for r in chk:
        print("  ", [c for c in r[:5]])


def ga4_run(service, dates: list[str], mode: str) -> None:
    """Записать лист GA4 — ровно теми числами, что лежат в витрине (lime_stats, web).

    До 2026-09-04 вкладка собиралась ОТДЕЛЬНЫМ запросом к GA4 (грандтотал `totalUsers` по
    дате×hostName). Пока витрина была на Метрике, это был независимый взгляд; после её
    перевода на GA4 (20.08) в книге оказалось ДВА разных GA4-числа: витрина/дашборд/
    недельный автоотчёт считают `activeUsers` по строкам разбивки, вкладка — `totalUsers`
    грандтоталом. Расхождение 2–4% (замер зонда W33: activeUsers сходится с ручным отчётом
    на 0.25%, totalUsers промахивается на +4.5%) выглядело как расхождение источников.
    Теперь GA4 в книге один — витрина; независимый источник для сверки = вкладка Метрики.
    """
    conn = psycopg2.connect(os.environ["DATABASE_URL"].split("?")[0], connect_timeout=30)
    try:
        mart = fetch_web(conn, dates)
    finally:
        conn.close()
    data = {iso: {s: {"org": m["web_org"], "paid": m["web_paid"]}
                  for s, m in day.items()} for iso, day in mart.items()}
    for iso in dates[-3:]:
        g = data[iso]["GCC"]
        print(f"{iso}: GA4 ORG {g['org']} PAID {g['paid']} Total {g['org'] + g['paid']}")
    if mode == "ga4-build":
        _build_tab(service, GA4_TAB, dates, data, header=_ga4_header(), row_fn=_ga4_row)
    else:
        _refresh_tab(service, GA4_TAB, dates, data, row_fn=_ga4_row)


def metrika_run(service, dates: list[str], mode: str) -> None:
    """Залить вкладку живой Метрики (ORG/PAID/Total по GCC и 5 странам) из витрины счётчиков.

    Форма колонок — как у GA4-вкладки (_ga4_header/_ga4_row), чтобы формулы сверки тянули
    оба источника одинаково: имя колонки ищется внутри своей вкладки.
    """
    conn = psycopg2.connect(os.environ["DATABASE_URL"].split("?")[0], connect_timeout=30)
    try:
        data = fetch_metrika_web(conn, dates)
    finally:
        conn.close()
    for iso in dates[-3:]:
        g = data[iso][_GCC]
        print(f"{iso}: Метрика ORG {g['org']} PAID {g['paid']} Total {g['org'] + g['paid']}")
    if mode == "metrika-build":
        _build_tab(service, METRIKA_TAB, dates, data, header=_ga4_header(), row_fn=_ga4_row)
    else:
        _refresh_tab(service, METRIKA_TAB, dates, data, row_fn=_ga4_row)


def ga4_cleanup(service) -> None:
    """Удалить из GA4-листа строки с датой < 2025-09-01 (мусор от слишком широкого refresh)."""
    from sync.sheets_write import delete_rows, read_values

    grid = read_values(service, SHEET_ID, f"{GA4_TAB}!A1:A4000", render="FORMATTED_VALUE")
    hdr_i = next((i for i, row in enumerate(grid) if row and _norm(row[0]) == "Дата"), 0)
    bad = []
    for i in range(hdr_i + 1, len(grid)):
        cell = grid[i][0] if grid[i] else ""
        m = _DATE_RE.search(cell)
        if m and f"{m[3]}-{m[2]}-{m[1]}" < "2025-09-01":
            bad.append(i)  # 0-based индекс строки = индекс в grid
    if bad:
        n = delete_rows(service, SHEET_ID, GA4_TAB, bad)
        print(f"ga4-cleanup: удалено строк с датой < 2025-09-01: {n}")
    else:
        print("ga4-cleanup: мусорных строк нет")


def main() -> None:
    mode = os.environ.get("LIME_GCC_MODE") or "refresh"
    if mode == "attr-compare":  # Meta: lastPlatformClick vs linearAll
        attr_compare()
        return
    if mode == "tw-order-dump":  # полная структура заказа TW
        tw_order_dump()
        return
    if mode == "tw-shopify-check":  # сверка заказов TW vs Shopify
        tw_shopify_check()
        return
    if mode == "app-orders-check":  # сверка app-заказов Shopify vs AppMetrica
        app_orders_check()
        return
    if mode == "metrika-probe":  # разведка regionCountry, Sheets не нужен
        metrika_probe()
        return
    if mode == "shopify-probe":  # проверка Shopify-токена, Sheets не нужен
        shopify_probe()
        return
    if mode == "app-probe":  # разведка AppMetrica GCC (Фаза 2), Sheets не нужен
        from sync.probe_gcc_app import main as app_probe
        app_probe()
        return
    if mode == "reporting-probe":  # только Reporting API (агрегат DAU/источник), без Logs → без 429
        from sync.probe_gcc_app import reporting_only
        reporting_only()
        return
    if mode == "app-validate":  # сверка app DAU по стране: Logs vs Reporting (=UI Аудитория)
        from sync.probe_gcc_app import app_validate
        app_validate()
        return
    if mode == "ga4-probe":  # разведка GA4: одно ли property на все страны + каналы
        from sync.gcc_ga4 import probe as ga4_probe
        ga4_probe()
        return
    if mode == "ga4-validate":  # сверка GA4-сбора с ручным файлом (per-country ORG/PAID/Total)
        from sync.gcc_ga4 import validate as ga4_validate
        ga4_validate()
        return
    if mode == "ga4-hosts":  # диагностика: все hostName за день (что не замаплено в страну)
        from sync.gcc_ga4 import hosts_probe
        hosts_probe()
        return
    if mode == "audit":  # аудит расхождений Метрика↔GA4 (покрытие + дневная динамика), Sheets не нужен
        from sync.gcc_audit import audit
        audit()
        return
    if mode == "ru-direct-check":  # RU: клики Директа vs Метрика DAU (родная пара), только БД
        from sync.gcc_audit import ru_direct_check
        ru_direct_check()
        return
    if mode == "page-data":  # данные для страницы «старый vs новый подход» (одно окно, обе методики)
        from sync.gcc_audit import page_data
        page_data()
        return

    from sync.sheets_write import get_write_service
    service = get_write_service()
    if mode == "ga4-cleanup":  # удалить мусорные строки < 2025-09-01 из GA4-листа
        ga4_cleanup(service)
        return
    if mode == "verify":  # самопроверка: книга содержит вчерашнюю дату
        verify(service)
        return
    if mode == "load-history":  # разовая заливка истории из CSV
        load_history(service)
        return
    if mode == "traffic-backfill":  # разовый перевод листа трафика GA4 → Метрика (весь период)
        traffic_backfill(service)
        return
    if mode == "app-traffic-fill":  # добор app-трафика (DAU) в лист, web сохраняется
        app_traffic_fill(service)
        return
    if mode == "probe":
        probe(service)
        return
    if mode == "ga4-build":  # создать GA4-лист + бэкфилл (LIME_GCC_FROM..вчера)
        to_env = os.environ.get("LIME_GCC_TO")
        to = date.fromisoformat(to_env) if to_env else _msk_today() - timedelta(days=1)
        ga4_run(service, _dates(date.fromisoformat(BUILD_FROM), to), "ga4-build")
        return
    if mode == "ga4-refresh":  # обновить последние N дней GA4-листа
        to = _msk_today() - timedelta(days=1)
        ga4_run(service, _dates(to - timedelta(days=REFRESH_DAYS - 1), to), "ga4-refresh")
        return
    if mode == "ga4-rebuild":  # перезаписать ВСЮ историю GA4-листа (даты уже есть → refresh их обновит)
        to_env = os.environ.get("LIME_GCC_TO")
        to = date.fromisoformat(to_env) if to_env else _msk_today() - timedelta(days=1)
        ga4_run(service, _dates(date.fromisoformat(BUILD_FROM), to), "ga4-refresh")
        return
    if mode == "metrika-build":  # создать вкладку живой Метрики + бэкфилл (LIME_GCC_FROM..вчера)
        to_env = os.environ.get("LIME_GCC_TO")
        to = date.fromisoformat(to_env) if to_env else _msk_today() - timedelta(days=1)
        metrika_run(service, _dates(date.fromisoformat(BUILD_FROM), to), "metrika-build")
        return
    if mode == "metrika-refresh":  # обновить последние N дней вкладки Метрики
        to = _msk_today() - timedelta(days=1)
        metrika_run(service, _dates(to - timedelta(days=REFRESH_DAYS - 1), to), "metrika-refresh")
        return
    if mode == "compare-formulas":  # создать лист сверки на формулах (GA4 всего vs Метрика DAU)
        ga4_compare_formulas(service)
        return
    if mode == "orders-lpc-build":  # разовый бэкфилл листа заказов по новой методике (LPC)
        orders_lpc_build(service)
        return
    if mode == "cr-compare-formulas":  # лист «Сверка CR web» было/стало на формулах
        cr_compare_formulas(service)
        return
    if mode == "compare-check":  # что реально посчитал лист сверки за окно LIME_GCC_FROM..TO
        from sync.sheets_write import read_values
        frm = os.environ.get("LIME_GCC_FROM") or ""
        to = os.environ.get("LIME_GCC_TO") or "9999-99-99"
        grid = read_values(service, SHEET_ID, f"{COMPARE_TAB}!A1:D4000", render="FORMATTED_VALUE")
        hdr_i = _header_row(grid) or 0
        print(" | ".join(grid[hdr_i][:4]))
        for row in grid[hdr_i + 1:]:
            m = _DATE_RE.search(row[0] if row else "")
            if m and frm <= f"{m[3]}-{m[2]}-{m[1]}" <= to:
                print("  " + " | ".join(str(c) for c in row[:4]))
        return
    if mode == "cr-check":  # диагностика листа сверки CR: значения + формулы
        from sync.sheets_write import read_values
        vals = read_values(service, SHEET_ID, f"{CR_TAB}!A1:F10", render="FORMATTED_VALUE")
        print(f"«{CR_TAB}» значения (A1:F10), строк: {len(vals)}")
        for r in vals:
            print("  ", r[:6])
        fml = read_values(service, SHEET_ID, f"{CR_TAB}!A2:C2", render="FORMULA")
        print("формулы A2:C2:")
        for r in fml:
            for c in r:
                print("  ", str(c)[:220])
        return
    if mode == "build":
        to_env = os.environ.get("LIME_GCC_TO")
        to = date.fromisoformat(to_env) if to_env else _msk_today() - timedelta(days=1)
        _run(service, _dates(date.fromisoformat(BUILD_FROM), to), "build")
    elif mode == "refresh":
        to = _msk_today() - timedelta(days=1)
        dates = _dates(to - timedelta(days=REFRESH_DAYS - 1), to)
        _run(service, dates, "refresh")
        # GA4-лист — best-effort в той же связке (производная витрины; сбой не рушит основной синк).
        try:
            ga4_run(service, dates, "ga4-refresh")
        except Exception as e:  # noqa: BLE001
            print(f"GA4 refresh ПРОПУЩЕН: {type(e).__name__}: {e}")
        # Вкладка живой Метрики — тоже best-effort: это независимый от GA4 счётчик, на нём
        # держатся оба листа сверки. Пустая/отставшая вкладка не должна ронять основной синк.
        try:
            metrika_run(service, dates, "metrika-refresh")
        except Exception as e:  # noqa: BLE001
            print(f"Метрика refresh ПРОПУЩЕН: {type(e).__name__}: {e}")
    else:
        raise SystemExit(f"lime_gcc_report: неизвестный режим {mode!r}")


if __name__ == "__main__":
    main()
