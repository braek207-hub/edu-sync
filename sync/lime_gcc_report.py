# -*- coding: utf-8 -*-
"""sync/lime_gcc_report.py — трафик и заказы GCC → Google-таблица (широкий формат).

Комбинация RU-формата (Web/App × Org/Paid) и GCC-разбивки по странам Залива. По каждому
срезу (весь GCC + 5 стран UAE/KSA/QA/KW/OM) семь колонок:
    Web Org · Web Paid · App Org · App Paid · WEB Total · APP Total · Total(«Общий» у GCC).

Источники:
  • WEB (трафик = sessions Метрики, заказы = purchases Triple Whale) — уже в lime_stats
    (region='gcc', data_source='web') с колонками country + traffic_type.
  • APP (app 6299245, трафик = sessions, заказы = e-commerce) — ФАЗА 2, пока нули.

«GCC»-срез = весь регион, включая country=NULL (визиты/заказы без страны), поэтому по
странам он НЕ сходится с суммой пяти — так в данных.

Две вкладки: «Fact Traffic GCC» (sessions) и «Fact Orders GCC» (purchases).

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
       COALESCE(SUM(sessions), 0)::bigint AS sessions
FROM lime_stats
WHERE region = 'gcc' AND data_source = 'web' AND date = ANY(%s::date[])
GROUP BY date, country, traffic_type
"""


def fetch_web(conn, dates: list[str]) -> dict:
    """traffic: date → {scope → metrics}. web-трафик (sessions) из lime_stats.

    Заказы здесь НЕ считаются — они берутся по атрибуции linearAll из TW+Shopify
    (fetch_orders_linear). app-трафик доливает AppMetrica в _run. GCC-срез = весь регион
    (в т.ч. country=NULL); коды — только 5 узнаваемых стран.
    """
    traffic = {d: _empty_day() for d in dates}
    with conn.cursor() as cur:
        cur.execute(_STATS_SQL, (dates,))
        for d, country, ttype, sess in cur.fetchall():
            field = "web_paid" if ttype == "Платный" else "web_org"
            code = _COUNTRY_CODE.get((country or "").strip())
            traffic[d][_GCC][field] += int(sess)
            if code:
                traffic[d][code][field] += int(sess)
    return traffic


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


def fetch_orders_linear(dates: list[str]) -> dict:
    """Заказы GCC по linearAll из TW+Shopify (канал web/app + страна доставки)."""
    from sync.gcc_shopify import fetch_order_meta
    from sync.gcc_triplewhale import fetch_tw_orders

    if not os.environ.get("GCC_TRIPLEWHALE_API_KEY"):
        print("orders: GCC_TRIPLEWHALE_API_KEY не задан — заказы 0")
        return {d: _empty_day() for d in dates}
    tw_key = os.environ["GCC_TRIPLEWHALE_API_KEY"]
    shop = os.environ["GCC_TW_SHOP_DOMAIN"]
    country_map, app_ids = fetch_order_meta(os.environ["API_LIME_SHOPIFY"], dates[0], dates[-1])
    by_date = {iso: fetch_tw_orders(tw_key, shop, iso, iso) for iso in dates}
    return aggregate_orders_linear(by_date, country_map, app_ids, dates)


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


def _run(service, dates: list[str], mode: str) -> None:
    conn = psycopg2.connect(os.environ["DATABASE_URL"].split("?")[0], connect_timeout=30)
    try:
        traffic = fetch_web(conn, dates)
    finally:
        conn.close()

    if os.environ.get("APPMETRICA_TOKEN"):
        from sync.gcc_app import fetch_app
        # AppMetrica — ТОЛЬКО app-трафик (sessions): заказы она недосчитывает (~43%).
        app_dates = dates if len(dates) <= APP_BACKFILL else dates[-APP_BACKFILL:]
        ta, _ = fetch_app(os.environ["APPMETRICA_TOKEN"], APP_ID, app_dates, APP_LOOKBACK)
        _merge_app(traffic, ta)
    else:
        print("gcc_app: APPMETRICA_TOKEN не задан — app-трафик 0")

    # Заказы (web+app) — по атрибуции linearAll из TW+Shopify, округлены с точным тоталом.
    orders = fetch_orders_linear(dates)

    for iso in dates:
        g, go = traffic[iso][_GCC], orders[iso][_GCC]
        print(f"{iso}: трафик web {g['web_org']}/{g['web_paid']} app {g['app_org']}/{g['app_paid']} | "
              f"заказы web {go['web_org']}/{go['web_paid']} app {go['app_org']}/{go['app_paid']}")
    for tab, data in ((TRAFFIC_TAB, traffic), (ORDERS_TAB, orders)):
        if mode == "build":
            _build_tab(service, tab, dates, data)
        else:
            _refresh_tab(service, tab, dates, data)


def _build_tab(service, tab: str, dates: list[str], data: dict) -> None:
    """Создать вкладку (если нет) и залить заголовок + все строки одним блоком."""
    from sync.sheets_write import add_tab, list_tabs, read_values, write_block

    tabs = set(list_tabs(service, SHEET_ID))
    if tab not in tabs:
        add_tab(service, SHEET_ID, tab)
        print(f"создана вкладка «{tab}»")
    else:
        existing = read_values(service, SHEET_ID, f"{tab}!A1:BZ5", render="FORMATTED_VALUE")
        if _header_row(existing) is not None:
            raise RuntimeError(f"{tab}: уже заполнена — build перезапишет от A1. Используй refresh.")

    header = _header()
    block = [header]
    for iso in dates:
        rv = _row_values(iso, data[iso])
        block.append([rv.get(c, "") for c in header])
    write_block(service, SHEET_ID, f"{tab}!A1", block)
    print(f"{tab}: залито {len(dates)} дн., {len(header)} колонок")


def _refresh_tab(service, tab: str, dates: list[str], data: dict) -> None:
    """Обновить строки последних дней по дате-матчу; новые даты дописать. По ИМЕНАМ колонок."""
    from sync.sheets_write import batch_write, read_values

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

    updates, written = [], []
    for iso in sorted(dates):
        ri = row_of.get(iso)
        is_new = ri is None
        if is_new:
            ri = next0
            next0 += 1
            row_of[iso] = ri
        rv = _row_values(iso, data[iso])
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
    print(f"{tab}: refresh {len(written)} дн., {n} ячеек")


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
    for tab in (TRAFFIC_TAB, ORDERS_TAB):
        try:
            vals = read_values(service, SHEET_ID, f"{tab}!A1:BZ6", render="FORMATTED_VALUE")
            print(f"\n=== {tab} ===")
            for i, row in enumerate(vals[:6]):
                print(f"  [{i}] {row}")
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

    from sync.sheets_write import get_write_service
    service = get_write_service()
    if mode == "load-history":  # разовая заливка истории из CSV
        load_history(service)
        return
    if mode == "probe":
        probe(service)
        return
    if mode == "build":
        to_env = os.environ.get("LIME_GCC_TO")
        to = date.fromisoformat(to_env) if to_env else _msk_today() - timedelta(days=1)
        _run(service, _dates(date.fromisoformat(BUILD_FROM), to), "build")
    elif mode == "refresh":
        to = _msk_today() - timedelta(days=1)
        _run(service, _dates(to - timedelta(days=REFRESH_DAYS - 1), to), "refresh")
    else:
        raise SystemExit(f"lime_gcc_report: неизвестный режим {mode!r}")


if __name__ == "__main__":
    main()
