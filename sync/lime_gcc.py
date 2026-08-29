# -*- coding: utf-8 -*-
"""sync/lime_gcc.py — оркестратор GCC: GA4(трафик) + Triple Whale(заказы/расход) → lime_stats (region='gcc').

Методика = ручной недельный отчёт Павла (решение 2026-08-20, план
docs/superpowers/plans/2026-08-20-lime-gcc-linearall-ga4.md в EDU v2):
- sync.gcc_ga4.fetch_ga4_dashboard_traffic — сессии/юзеры/воронка (GA4 property 417919368);
  до 2026-08-20 трафик брала Яндекс.Метрика 98232701 (модуль gcc_metrika остаётся для аудита)
- sync.gcc_triplewhale.aggregate_orders_by_channel — заказы/выручка по **linearAll**
  (1/N на касание, дробно; AED)
- sync.gcc_triplewhale.spend_by_channel — расход (TW summary-page, AED)
Деньги (cost/revenue) конвертируются AED→RUB по курсу ЦБ (sync.fx.to_rub); трафик — как есть.

Каналы каждого источника уже приведены к единой таксономии (sync.gcc_channels): GA4 через
map_ga4_channel, TW-заказы/расход через map_tw_source/SPEND_METRIC_MAP — поэтому мерж по
(channel, subchannel) валиден.

Заказы пишутся ДРОБНЫМИ: linearAll кладёт 1/N заказа на касание, и ручной отчёт агентства
так их и показывает (157,2 у All Paid, 9 026 у канала). Колонка purchases_count переведена
в double precision миграцией 20260825170000 — до неё дробность гасилась largest remainder
внутри (день, страна), из-за чего раскладка по каналам и кампаниям гуляла на ±2 заказа.

customers/new_customers/new_customers_revenue = 0: TW-атрибуция не даёт чистого
per-channel деления новый/лояльный клиент — блок «Новые/Лояльные» GCC пуст в v1.

ENV: GOOGLE_APPLICATION_CREDENTIALS/LIME_REPORTS_SA_JSON (SA lime-reports, scope analytics),
GCC_GA4_PROPERTY (default 417919368), GCC_TRIPLEWHALE_API_KEY, GCC_TW_SHOP_DOMAIN,
DATABASE_URL, LIME_GCC_SYNC_FROM/LIME_GCC_SYNC_TO или LIME_GCC_SYNC_DAYS (default 7).
LIME_GCC_DRY_RUN — пропустить БД, только напечатать сводку (для локальной проверки
без доступа к прод-Supabase с машины).
Запуск: python -m sync.lime_gcc
"""
import os
import time
from datetime import date, timedelta

import psycopg2
import psycopg2.extras

from sync.fx import to_rub as fx_to_rub
from sync.gcc_campaign_bridge import bridge_metrika_campaign, fetch_campaign_index
from sync.gcc_ga4 import GA4_PROPERTY, fetch_ga4_dashboard_traffic
from sync.gcc_google_geo import fetch_geo_spend
from sync.gcc_tw_ads import fetch_ads_spend, spend_metrics_covered
from sync.gcc_triplewhale import aggregate_orders_by_channel, fetch_tw_orders, fetch_tw_spend, spend_by_channel

SYNC_DAYS = int(os.environ.get("LIME_GCC_SYNC_DAYS") or "7")

DELETE_SQL = "DELETE FROM lime_stats WHERE region = 'gcc' AND date >= %s AND date <= %s"

# Порядок колонок = порядок полей в кортежах merge_rows(). Держим одним списком, чтобы
# добавление колонки не разъезжалось с индексами в сводке dry-run.
COLUMNS = (
    "date", "data_source", "region", "country", "channel", "subchannel", "traffic_type",
    "campaign_id", "campaign_name",
    "cost", "clicks", "impressions", "sessions", "users", "clients",
    "purchases_count", "purchases_revenue", "customers",
    "new_users", "new_customers", "new_customers_revenue",
    "bounce_rate", "page_depth", "cart_reaches", "checkout_reaches",
)

INSERT_SQL = f"INSERT INTO lime_stats ({', '.join(COLUMNS)}) VALUES %s"


def merge_rows(ga4_rows, tw_order_rows, tw_spend_rows, fx_rate, date_s,
               rub_spend_rows=(), campaign_index=None) -> list[tuple]:
    """Свернуть трафик (GA4) + заказы/расход (Triple Whale) по (country, channel, subchannel).

    Страна берётся из самого источника: GA4 — хост витрины, TW-заказы — Shopify/journey.
    Источники без гео-разбивки (расход из TW summary-page — он на весь магазин) дают
    country=None: такие строки не приписываются ни одной стране, но входят в GCC-тотал.

    Args:
        ga4_rows: sync.gcc_ga4.fetch_ga4_dashboard_traffic() за день date_s.
        tw_order_rows: sync.gcc_triplewhale.aggregate_orders_by_channel() за день date_s
            (заказы дробные, linearAll).
        tw_spend_rows: sync.gcc_triplewhale.spend_by_channel() за день date_s (в AED).
        fx_rate: курс AED→RUB (sync.fx.to_rub("AED", date_s)).
        date_s: дата строк (YYYY-MM-DD).
        rub_spend_rows: расход, УЖЕ пересчитанный в рубли (гео-расход Google из кабинета,
            sync.gcc_google_geo) — курс к нему не применяется повторно.

    Returns:
        Список кортежей в порядке COLUMNS, по одному на (country, channel, subchannel).
    """
    agg: dict[tuple[str | None, str, str, str], dict] = {}

    def _bucket(country, campaign, channel, subchannel, traffic_type):
        # Кампания в ключе: id одинаков в GA4 (sessionCampaignId), attribution TW и кабинете,
        # поэтому визиты, заказы и расход одной кампании сходятся в одну строку.
        key = (country, campaign or "", channel, subchannel)
        row = agg.get(key)
        if row is None:
            row = {
                "traffic_type": traffic_type,
                "campaign_name": "",
                "sessions": 0,
                "users": 0,
                "orders": 0,
                "revenue": 0.0,
                "cost": 0.0,
                "cost_rub": 0.0,
                "new_users": 0,
                # Взвешенные на визиты — иначе среднее от средних соврёт при склейке строк.
                "bounce_w": 0.0,
                "depth_w": 0.0,
                "cart": 0,
                "checkout": 0,
            }
            agg[key] = row
        elif not row["traffic_type"]:
            row["traffic_type"] = traffic_type
        return row

    for m in ga4_rows:
        # GA4 отдаёт числовой sessionCampaignId (Google И Meta) — он же в заказах TW и
        # ads_table, мост не нужен. Когда id пуст, а метка есть (письма, ручные utm) —
        # переводим метку в id мостом; неопознанные метки остаются как есть.
        campaign = m.get("campaign") or bridge_metrika_campaign(
            m.get("campaign_label"), campaign_index or {})
        row = _bucket(m.get("country"), campaign, m["channel"], m["subchannel"],
                      m["traffic_type"])
        row["sessions"] += int(m["visits"] or 0)
        row["users"] += int(m["users"] or 0)
        row["new_users"] += int(m.get("new_users") or 0)
        row["bounce_w"] += float(m.get("bounce_w") or 0)
        row["depth_w"] += float(m.get("depth_w") or 0)
        row["cart"] += int(m.get("cart_reaches") or 0)
        row["checkout"] += int(m.get("checkout_reaches") or 0)

    for o in tw_order_rows:
        row = _bucket(o.get("country"), o.get("campaign"), o["channel"], o["subchannel"],
                      o.get("traffic_type"))
        row["orders"] += float(o["orders"] or 0)
        row["revenue"] += float(o["revenue"] or 0)

    for sp in tw_spend_rows:
        # Два вида строк: из summary-page (весь магазин, country/campaign = None) и из
        # ads_table по кампаниям (страна выведена из имени кампании). Ключ берём как есть —
        # первые садятся в тотал GCC, вторые на свою страну и кампанию.
        row = _bucket(sp.get("country"), sp.get("campaign_id"), sp["channel"],
                      sp["subchannel"], sp.get("traffic_type"))
        row["cost"] += float(sp["cost"] or 0)
        if sp.get("campaign_name") and not row["campaign_name"]:
            row["campaign_name"] = sp["campaign_name"]

    for sp in rub_spend_rows:
        # Уже в рублях (гео-расход Google из кабинета) — в отдельную корзину, мимо курса.
        row = _bucket(sp.get("country"), sp.get("campaign_id"), sp["channel"], sp["subchannel"],
                      sp.get("traffic_type"))
        row["cost_rub"] += float(sp["cost"] or 0)
        # Имя кампании знает только кабинет — Метрика и TW отдают голый id.
        if sp.get("campaign_name") and not row["campaign_name"]:
            row["campaign_name"] = sp["campaign_name"]

    out: list[tuple] = []
    for key, row in agg.items():
        country, campaign, channel, subchannel = key
        cost_rub = round(row["cost"] * fx_rate + row["cost_rub"], 2)
        revenue_rub = round(row["revenue"] * fx_rate, 2)
        sessions = row["sessions"]
        out.append((
            date_s, "web", "gcc", country, channel, subchannel, row["traffic_type"],
            campaign, row["campaign_name"],                    # campaign_id, campaign_name
            cost_rub, 0, 0, row["sessions"], row["users"], 0,   # cost, clicks, impressions, sessions, users, clients
            round(float(row["orders"]), 6), revenue_rub, 0,     # purchases_count, purchases_revenue, customers
            row["new_users"], 0, 0.0,                           # new_users, new_customers, new_customers_revenue
            # Средневзвешенные по визитам; проценты и «страниц за визит» — конвенция
            # polinarepik_metrica_visits, хендлер взвешивает обратно (SUM(x * sessions)).
            round(row["bounce_w"] / sessions * 100, 4) if sessions else None,
            round(row["depth_w"] / sessions, 4) if sessions else None,
            row["cart"], row["checkout"],
        ))
    return out


def app_order_rows(app_agg: list[dict], fx_rate: float, date_s: str) -> list[tuple]:
    """Заказы приложения (Shopify app-канал) → строки lime_stats data_source='app'.

    Только заказы/выручка (трафика тут нет — app-трафик считает AppMetrica). Атрибуция
    (paid/organic, linearAll) и страна доставки — те же, что у web (из TW/Shopify), но
    канал app. Заказы дробные, как у web.
    """
    pseudo_agg = {(o.get("country"), i): o for i, o in enumerate(app_agg)}
    out: list[tuple] = []
    for key, o in pseudo_agg.items():
        out.append((
            date_s, "app", "gcc", o.get("country"), o["channel"], o["subchannel"],
            o.get("traffic_type"), o.get("campaign"), "",
            0.0, 0, 0, 0, 0, 0,                                    # cost, clicks, impressions, sessions, users, clients
            round(float(o["orders"] or 0), 6), round(float(o["revenue"] or 0) * fx_rate, 2), 0,
            0, 0, 0.0, None, None, 0, 0,
        ))
    return out


GRAIN_LEN = 9  # date..campaign_name — зерно витрины дашборда (его GROUP BY)
SUM_COLS = ("cost", "clicks", "impressions", "sessions", "users", "clients",
            "purchases_count", "purchases_revenue", "customers",
            "new_users", "new_customers", "new_customers_revenue",
            "cart_reaches", "checkout_reaches")
# Средние на визит: складывать их нельзя, только пересреднять по визитам.
AVG_COLS = ("bounce_rate", "page_depth")


def fold_by_grain(rows: list[tuple]) -> list[tuple]:
    """Свернуть строки одного дня по зерну витрины (date..campaign_name).

    Зачем. Заказы приложения (app_order_rows) и его трафик (app_traffic_tuples) приходят
    разными строками: у первой заполнены заказы и выручка, у второй — визиты и юзеры.
    Когда кампании нет (органика, Direct/SEO), обе ложатся на ОДНО зерно и до 29.08.2026
    уезжали в базу двумя строками. Замер на проде: 14 таких пар из 384 360 строк —
    единственное место во всей таблице, где зерно не уникально, и потому единственная
    причина, по которой дашборд обязан делать GROUP BY (он стоит 92 % времени запроса).

    Складываем здесь, а не в дашборде: строка «заказы без визитов» рядом со строкой
    «визиты без заказов» — не два факта, а один, разрезанный источником.

    Средние на визит (отказы, глубина) пересредняются по визитам: у строки заказов их нет
    (None), у строки трафика есть — простое сложение дало бы сумму долей.
    """
    order = {name: i for i, name in enumerate(COLUMNS)}
    i_sessions = order["sessions"]
    folded: dict[tuple, list] = {}
    for row in rows:
        key = row[:GRAIN_LEN]
        acc = folded.get(key)
        if acc is None:
            folded[key] = list(row)
            continue
        # Веса средних снимаем ДО сложения визитов: после него acc[sessions] — уже сумма,
        # и пересреднение считало бы по неверному весу.
        wa, wb = acc[i_sessions] or 0, row[i_sessions] or 0
        for name in SUM_COLS:
            i = order[name]
            acc[i] = (acc[i] or 0) + (row[i] or 0)
        for name in AVG_COLS:
            i = order[name]
            a, b = acc[i], row[i]
            if b is None:
                continue
            if a is None:
                acc[i] = b
                continue
            acc[i] = round((a * wa + b * wb) / (wa + wb), 4) if wa + wb else a
    return [tuple(v) for v in folded.values()]


# Приложение LIME International (AppMetrica); запущено ~2026-06-02 — раньше строк app нет.
APP_ID = os.environ.get("GCC_APP_ID") or "6299245"
APP_TRAFFIC_FLOOR = "2026-06-02"
APP_LOOKBACK = int(os.environ.get("LIME_GCC_APP_LOOKBACK_DAYS") or "90")


def app_traffic_tuples(traffic_rows: list[dict]) -> dict[str, list[tuple]]:
    """Строки app-трафика (gcc_app.build_app_traffic_rows) → кортежи lime_stats по дням.

    Только users/sessions (заказы app в отдельных строках из TW, app_order_rows).
    """
    by_day: dict[str, list[tuple]] = {}
    for r in traffic_rows:
        by_day.setdefault(r["date"], []).append((
            r["date"], "app", "gcc", r["country"], r["channel"], r["subchannel"],
            r["traffic_type"], None, "",
            0.0, 0, 0, int(r.get("sessions") or 0), int(r["users"] or 0), 0,
            0, 0.0, 0,
            0, 0, 0.0, None, None, 0, 0,
        ))
    return by_day


def _fetch_app_traffic_by_day(frm: date, to: date) -> dict[str, list[tuple]]:
    """App-трафик за диапазон (best-effort): {день: кортежи}. Нет токена/данных → {}."""
    token = os.environ.get("APPMETRICA_TOKEN")
    if not token:
        print("lime_gcc: APPMETRICA_TOKEN не задан — app-трафик не пишем")
        return {}
    floor = date.fromisoformat(APP_TRAFFIC_FLOOR)
    if to < floor:
        return {}
    app_frm = max(frm, floor)
    dates = []
    d = app_frm
    while d <= to:
        dates.append(d.isoformat())
        d += timedelta(days=1)
    try:
        from sync.gcc_app import fetch_app_traffic_dashboard
        rows = fetch_app_traffic_dashboard(token, APP_ID, dates, APP_LOOKBACK)
    except Exception as e:  # noqa: BLE001 — app-трафик не должен ронять заказы/web
        print(f"lime_gcc: app-трафик ПРОПУЩЕН ({type(e).__name__}: {e})")
        return {}
    print(f"lime_gcc: app-трафик — {len(rows)} строк за {dates[0]}…{dates[-1]}")
    return app_traffic_tuples(rows)


DEADLOCK_RETRIES = 4
DEADLOCK_BACKOFF_SEC = 3


def _write_day(conn, day_s: str, rows: list[tuple]) -> None:
    """DELETE+INSERT за день с повтором при дедлоке.

    В `lime_stats` пишут несколько синков (GCC, kz_metrika, витрина), и Postgres ловит
    взаимную блокировку на пересечении индексов: бэкфилл 2026-07-19 упал на 236-м дне из
    245 с `DeadlockDetected`, потеряв час прогона. Жертву транзакции достаточно повторить —
    операция идемпотентна (удаляем и пишем ровно свой день своего региона).
    """
    for attempt in range(1, DEADLOCK_RETRIES + 1):
        try:
            with conn.cursor() as cur:
                cur.execute(DELETE_SQL, (day_s, day_s))
                if rows:
                    psycopg2.extras.execute_values(cur, INSERT_SQL, rows, page_size=500)
            conn.commit()
            return
        except (psycopg2.errors.DeadlockDetected,
                psycopg2.errors.LockNotAvailable) as exc:
            conn.rollback()
            if attempt == DEADLOCK_RETRIES:
                raise
            sleep_s = DEADLOCK_BACKOFF_SEC * attempt
            print(f"lime_gcc: {day_s} — {type(exc).__name__}, "
                  f"попытка {attempt}/{DEADLOCK_RETRIES}, повтор через {sleep_s}с")
            time.sleep(sleep_s)


def _month_spans(frm: date, to: date) -> list[tuple[date, date]]:
    """Разбить период на календарные месяцы (границы включительно).

    Помесячно, а не одним куском: у GA4 runReport limit=250000 строк на ответ, и на длинном
    диапазоне с шестью измерениями выдача упёрлась бы в него молча — без ошибки, просто
    обрезанная. Месяц GCC даёт ~4 тысячи строк, запас многократный.
    """
    spans: list[tuple[date, date]] = []
    start = frm
    while start <= to:
        if start.month == 12:
            next_month = date(start.year + 1, 1, 1)
        else:
            next_month = date(start.year, start.month + 1, 1)
        end = min(next_month - timedelta(days=1), to)
        spans.append((start, end))
        start = end + timedelta(days=1)
    return spans


def _fetch_ga4_by_month(frm: date, to: date) -> dict[str, list[dict]]:
    """Трафик GA4 за весь период помесячно, разложенный по дням.

    Returns:
        {"YYYY-MM-DD": [строки fetch_ga4_dashboard_traffic за этот день]}.
        Дни без трафика в словаре отсутствуют — вызывающий берёт пустой список.
    """
    by_day: dict[str, list[dict]] = {}
    spans = _month_spans(frm, to)
    for i, (span_from, span_to) in enumerate(spans, 1):
        rows = fetch_ga4_dashboard_traffic(
            GA4_PROPERTY, span_from.isoformat(), span_to.isoformat()
        )
        for row in rows:
            by_day.setdefault(row["date"], []).append(row)
        print(f"lime_gcc: GA4 {span_from}…{span_to} → {len(rows)} строк "
              f"(месяц {i} из {len(spans)})")
    return by_day


def _sync_range(frm: date, to: date, conn) -> int:
    tw_key = os.environ["GCC_TRIPLEWHALE_API_KEY"]
    shop = os.environ["GCC_TW_SHOP_DOMAIN"]

    # Справочник кампаний строим ОДИН раз на весь прогон и с запасом назад: у дневного
    # синка в своём дне может не быть расхода по кампании, чей трафик уже идёт, и метка
    # осталась бы неопознанной.
    campaign_index = fetch_campaign_index(
        tw_key, shop, (frm - timedelta(days=90)).isoformat(), to.isoformat()
    )
    print(f"lime_gcc: справочник кампаний — {len(campaign_index)} имён")

    # Страна заказа — из Shopify (адрес доставки), не из домена витрины: люди заходят на
    # витрину одной страны, а доставка в другую. Тянем раз на весь диапазон, джойн по
    # order_id в aggregate_orders_by_channel. Без токена — мапа пустая, fallback на домен.
    shopify_country: dict[str, str | None] = {}
    app_orders: set[str] = set()
    if os.environ.get("API_LIME_SHOPIFY"):
        from sync.gcc_shopify import fetch_order_meta
        shopify_country, app_orders = fetch_order_meta(
            os.environ["API_LIME_SHOPIFY"], frm.isoformat(), to.isoformat()
        )
        print(f"lime_gcc: Shopify — {len(shopify_country)} заказов, из них app-канал {len(app_orders)} "
              f"(исключены из web, они в app из AppMetrica)")
    else:
        print("lime_gcc: API_LIME_SHOPIFY не задан — страна заказов по домену витрины, app не исключён")

    # GA4 тянем ПОМЕСЯЧНО (2 запроса на месяц: трафик + воронка), `date` стоит в
    # измерениях — диапазон возвращает те же построчные данные, что и по дню.
    ga4_by_day = _fetch_ga4_by_month(frm, to)
    app_traffic_by_day = _fetch_app_traffic_by_day(frm, to)

    total = 0
    day = frm
    while day <= to:
        day_s = day.isoformat()
        traffic = ga4_by_day.get(day_s, [])
        tw_orders_raw = fetch_tw_orders(tw_key, shop, day_s, day_s)
        # web-срез (Online Store, app исключён) и app-срез (только app-канал) — оба с
        # TW-атрибуцией и Shopify-страной. Канал web/app знает только Shopify.
        orders = aggregate_orders_by_channel(tw_orders_raw, day_s,
                                             country_by_order=shopify_country, exclude_orders=app_orders)
        app_ord = aggregate_orders_by_channel(tw_orders_raw, day_s,
                                              country_by_order=shopify_country, only_orders=app_orders)
        tw_metrics = fetch_tw_spend(tw_key, shop, day_s)
        # Расход Google берём из кабинета (разложен по странам), если Script там уже стоит;
        # тогда ga_adCost из TW выбрасываем — иначе один и тот же расход посчитается дважды.
        google_geo = fetch_geo_spend(conn, day_s)
        if google_geo:
            tw_metrics = {k: v for k, v in tw_metrics.items() if k != "ga_adCost"}
        # Расход прочих площадок (Meta и др.) — по кампаниям из SQL-эндпоинта TW: там
        # есть имя кампании, а оно несёт страну. Перекрытые метрики выбрасываем из
        # summary-page, иначе тот же расход посчитается дважды.
        ads_spend = fetch_ads_spend(tw_key, shop, day_s, day_s)
        if ads_spend:
            covered = spend_metrics_covered()
            tw_metrics = {k: v for k, v in tw_metrics.items() if k not in covered}
        spend = spend_by_channel(tw_metrics, day_s) + ads_spend
        fx_rate = fx_to_rub("AED", day_s)
        rows = merge_rows(traffic, orders, spend, fx_rate, day_s,
                          rub_spend_rows=google_geo, campaign_index=campaign_index)
        rows += app_order_rows(app_ord, fx_rate, day_s)
        rows += app_traffic_by_day.get(day_s, [])
        rows = fold_by_grain(rows)

        if conn is None:
            i_country = COLUMNS.index("country")
            i_cost = COLUMNS.index("cost")
            i_revenue = COLUMNS.index("purchases_revenue")
            i_orders = COLUMNS.index("purchases_count")
            i_sessions = COLUMNS.index("sessions")
            cost_sum = sum(r[i_cost] for r in rows)
            revenue_sum = sum(r[i_revenue] for r in rows)
            orders_sum = sum(r[i_orders] for r in rows)
            print(f"lime_gcc: [DRY-RUN] {day_s} → {len(rows)} строк "
                  f"(cost={cost_sum:.2f}₽, revenue={revenue_sum:.2f}₽, orders={orders_sum})")
            by_country: dict[str | None, list[float]] = {}
            for r in rows:
                acc = by_country.setdefault(r[i_country], [0, 0.0, 0, 0.0])
                acc[0] += r[i_sessions]
                acc[1] += r[i_revenue]
                acc[2] += r[i_orders]
                acc[3] += r[i_cost]
            for country, (sessions, revenue, orders_n, cost) in sorted(
                by_country.items(), key=lambda kv: -kv[1][0]
            ):
                print(f"    {str(country or '(тотал GCC)'):<22} визиты={sessions:<7} "
                      f"заказы={orders_n:<5} выручка={revenue:.0f}₽ расход={cost:.0f}₽")
        else:
            _write_day(conn, day_s, rows)
            print(f"lime_gcc: {day_s} → {len(rows)} строк")

        total += len(rows)
        day += timedelta(days=1)
    return total


def _read_preserved(conn, day_s: str) -> tuple[list[dict], list[tuple]]:
    """Существующие строки region=gcc за день: web-заказы/расход (без трафика) + app целиком.

    Для режима regeo: заказы/расход НЕ перезапрашиваем у TW/Shopify (Павел: «заказы не трогаем»,
    Shopify отдаёт страну только за ~60 дней) — берём как есть. Трафик (sessions/users/…) из
    web-строк отбрасываем, его даёт свежий GA4. app-строки (трафика там нет) — целиком.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM lime_stats WHERE region='gcc' AND date=%s", (day_s,))
        existing = cur.fetchall()
    web, app_rows = [], []
    for r in existing:
        if r["data_source"] == "app":
            app_rows.append(tuple(r[c] for c in COLUMNS))
        else:
            web.append({
                "country": r["country"], "campaign": r["campaign_id"],
                "channel": r["channel"], "subchannel": r["subchannel"],
                "traffic_type": r["traffic_type"], "campaign_name": r["campaign_name"] or "",
                "orders": float(r["purchases_count"] or 0),
                "revenue_rub": float(r["purchases_revenue"] or 0),
                "cost_rub": float(r["cost"] or 0),
            })
    return web, app_rows


def _merge_regeo(ga4_rows, preserved_web, day_s, campaign_index) -> list[tuple]:
    """Свежий трафик (GA4) + СОХРАНЁННЫЕ заказы/расход (из lime_stats, уже в рублях)."""
    agg: dict[tuple, dict] = {}

    def bucket(country, campaign, channel, subchannel, tt):
        key = (country, campaign or "", channel, subchannel)
        row = agg.get(key)
        if row is None:
            row = {"traffic_type": tt, "campaign_name": "", "sessions": 0, "users": 0,
                   "new_users": 0, "bounce_w": 0.0, "depth_w": 0.0, "cart": 0, "checkout": 0,
                   "orders": 0, "revenue_rub": 0.0, "cost_rub": 0.0}
            agg[key] = row
        elif not row["traffic_type"]:
            row["traffic_type"] = tt
        return row

    for m in ga4_rows:
        channel, subchannel, tt = m["channel"], m["subchannel"], m["traffic_type"]
        campaign = m.get("campaign") or bridge_metrika_campaign(
            m.get("campaign_label"), campaign_index or {})
        row = bucket(m.get("country"), campaign, channel, subchannel, tt)
        row["sessions"] += int(m["visits"] or 0)
        row["users"] += int(m["users"] or 0)
        row["new_users"] += int(m.get("new_users") or 0)
        row["bounce_w"] += float(m.get("bounce_w") or 0)
        row["depth_w"] += float(m.get("depth_w") or 0)
        row["cart"] += int(m.get("cart_reaches") or 0)
        row["checkout"] += int(m.get("checkout_reaches") or 0)

    for p in preserved_web:
        row = bucket(p["country"], p["campaign"], p["channel"], p["subchannel"], p["traffic_type"])
        row["orders"] += p["orders"]
        row["revenue_rub"] += p["revenue_rub"]
        row["cost_rub"] += p["cost_rub"]
        if p["campaign_name"] and not row["campaign_name"]:
            row["campaign_name"] = p["campaign_name"]

    out = []
    for (country, campaign, channel, subchannel), row in agg.items():
        sessions = row["sessions"]
        out.append((
            day_s, "web", "gcc", country, channel, subchannel, row["traffic_type"],
            campaign, row["campaign_name"],
            round(row["cost_rub"], 2), 0, 0, row["sessions"], row["users"], 0,
            row["orders"], round(row["revenue_rub"], 2), 0,
            row["new_users"], 0, 0.0,
            round(row["bounce_w"] / sessions * 100, 4) if sessions else None,
            round(row["depth_w"] / sessions, 4) if sessions else None,
            row["cart"], row["checkout"],
        ))
    return out


def _regeo_range(frm: date, to: date, conn, dry: bool = False) -> int:
    """Ре-ингест ТОЛЬКО web-трафика за период. Заказы/расход/app — из lime_stats как есть."""
    tw_key = os.environ["GCC_TRIPLEWHALE_API_KEY"]
    shop = os.environ["GCC_TW_SHOP_DOMAIN"]
    campaign_index = fetch_campaign_index(
        tw_key, shop, (frm - timedelta(days=90)).isoformat(), to.isoformat())
    print(f"regeo: справочник кампаний — {len(campaign_index)} имён")
    ga4_by_day = _fetch_ga4_by_month(frm, to)

    total, day = 0, frm
    while day <= to:
        day_s = day.isoformat()
        traffic = ga4_by_day.get(day_s, [])
        web_pres, app_rows = _read_preserved(conn, day_s)
        rows = _merge_regeo(traffic, web_pres, day_s, campaign_index) + app_rows
        if dry:
            m_users = sum(int(m["users"] or 0) for m in traffic)
            o_sum = sum(p["orders"] for p in web_pres)
            print(f"regeo [DRY] {day_s}: трафик users {m_users}, сохр. заказов {o_sum} "
                  f"({len(web_pres)} web-строк), app {len(app_rows)} → {len(rows)} строк")
        else:
            _write_day(conn, day_s, rows)
            print(f"regeo {day_s} → {len(rows)} строк")
        total += len(rows)
        day += timedelta(days=1)
    return total


def sync_lime_gcc() -> int:
    frm_env = os.environ.get("LIME_GCC_SYNC_FROM")
    to_env = os.environ.get("LIME_GCC_SYNC_TO")
    if frm_env and to_env:
        frm = date.fromisoformat(frm_env)
        to = date.fromisoformat(to_env)
    else:
        to = date.today()
        frm = to - timedelta(days=SYNC_DAYS - 1)

    dry_run = bool(os.environ.get("LIME_GCC_DRY_RUN"))

    # regeo: ре-ингест только web-трафика на гео (нужен DB даже для dry — читаем заказы).
    if os.environ.get("LIME_GCC_MODE") == "regeo":
        url = os.environ["DATABASE_URL"].split("?")[0]
        conn = psycopg2.connect(url, connect_timeout=30)
        try:
            return _regeo_range(frm, to, conn, dry=dry_run)
        finally:
            conn.close()

    if dry_run or not os.environ.get("DATABASE_URL"):
        return _sync_range(frm, to, None)

    url = os.environ["DATABASE_URL"].split("?")[0]
    conn = psycopg2.connect(url, connect_timeout=30)
    try:
        total = _sync_range(frm, to, conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return total


if __name__ == "__main__":
    sync_lime_gcc()
