"""LIME · AppMetrica: агрегация сырья Logs API в витрины дашборда.

installs → недельные установки по партнёру (уникальные устройства, первая установка).
cohorts  → устройства-покупатели по когорте (месяц × партнёр) накопительно по месяцам жизни.
Сырьё не хранится — только агрегаты. Границы недели/месяца — по времени установки/события.
"""
import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs

import psycopg2
import psycopg2.extras

from sync.appmetrica_logs import fetch_installations, fetch_purchase_events


def parse_dt(s: str) -> datetime:
    # Logs API: 'YYYY-MM-DD HH:MM:SS' (иногда без секунд — подстрахуемся).
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"нераспознанный datetime: {s!r}")


def iso_monday(dt: datetime) -> date:
    d = dt.date()
    return date.fromordinal(d.toordinal() - d.weekday())


def month_start(dt: datetime) -> date:
    return date(dt.year, dt.month, 1)


def month_diff(later: date, earlier: date) -> int:
    return (later.year - earlier.year) * 12 + (later.month - earlier.month)


def _truthy(v) -> bool:
    return str(v).strip() in ("1", "true", "True", "yes")


def first_install_per_device(installs: list[dict], keep_reattribution: bool,
                             keep_reinstall: bool) -> dict[str, dict]:
    best: dict[str, dict] = {}
    for r in installs:
        if not keep_reinstall and _truthy(r.get("is_reinstallation")):
            continue
        if not keep_reattribution and _truthy(r.get("is_reattribution")):
            continue
        dev = r.get("appmetrica_device_id")
        if not dev:
            continue
        dt = parse_dt(r["install_datetime"])
        cur = best.get(dev)
        if cur is None or dt < cur["install_dt"]:
            best[dev] = {"install_dt": dt, "publisher": r.get("publisher_name") or "unknown"}
    return best


def param_of(raw: str, key: str) -> str:
    """Значение параметра ссылки трекера. Нет параметра → пустая строка."""
    if not raw:
        return ""
    try:
        q = parse_qs(raw)
    except Exception:
        return ""
    vals = q.get(key)
    return (vals[0] if vals else "")[:64]


def build_installs_daily(installs: list[dict], keep_reattribution: bool,
                         keep_reinstall: bool) -> list[tuple]:
    """Установки дня = уникальные устройства, установившие ИМЕННО в этот день.

    Грань дневная, потому что главная таблица дашборда суммирует дневные строки по
    произвольному диапазону дат — недельная метрика на таком диапазоне ломается
    (диапазон режет неделю пополам). Виджет APP сворачивает дни в недели сам.

    Дедуп локальный, внутри (день × партнёр), а НЕ по первой установке за всё окно:
    иначе устройство, поставившее приложение повторно, выпадало бы из свежих периодов
    (сверка со слайдами: глобальный дедуп давал −606 на неделе 29.06, локальный +206).
    Разница дневного дедупа и недельного пренебрежима (~41 устройство за 13 недель) —
    калибровка по слайдам сохраняется. Когорты живут на первой установке (build_cohorts).

    Детализация: за устройством закрепляются utm_source и campaign_id самой ранней его
    установки в этом дне у этого партнёра — иначе устройство с двумя установками под
    разными метками попало бы в две строки, и детали не сложились бы в родителя.

    campaign_id есть только у Директа (~96%); у VK в трекере лежит группа объявлений,
    а не кампания, поэтому там пусто — метрика ляжет на грань канала.

    Возвращает (date, publisher, detail, campaign_id, installs).
    """
    # (date, publisher, device) → (самая ранняя дата, utm_source, campaign_id)
    first_in_day: dict[tuple, tuple] = {}
    for r in installs:
        if not keep_reinstall and _truthy(r.get("is_reinstallation")):
            continue
        if not keep_reattribution and _truthy(r.get("is_reattribution")):
            continue
        dev = r.get("appmetrica_device_id")
        if not dev:
            continue
        dt = parse_dt(r["install_datetime"])
        key = (dt.date(), r.get("publisher_name") or "unknown", dev)
        cur = first_in_day.get(key)
        if cur is None or dt < cur[0]:
            params = r.get("click_url_parameters") or ""
            first_in_day[key] = (dt, param_of(params, "utm_source"),
                                 param_of(params, "campaign_id"))

    agg: dict[tuple, int] = defaultdict(int)
    for (day, pub, _dev), (_dt, detail, campaign) in first_in_day.items():
        agg[(day, pub, detail, campaign)] += 1
    return [(d, p, det, c, n) for (d, p, det, c), n in agg.items()]


def purchase_facts(events: list[dict]) -> list[tuple]:
    """Сырые события покупки → факты (device, месяц покупки, transaction_id, сумма).

    Дедуп по transaction_id: одно и то же событие иногда приходит дважды (0.4% на
    замере), и без дедупа задваивались бы и заказы, и выручка. Событие без
    transaction_id считаем отдельным заказом (ключ = None + позиция).
    """
    seen: set = set()
    out: list[tuple] = []
    for e in events:
        dev = e.get("appmetrica_device_id")
        if not dev:
            continue
        try:
            payload = json.loads(e.get("event_json") or "{}")
        except (ValueError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        txn = payload.get("transaction_id")
        txn = str(txn) if txn is not None else None
        if txn is not None:
            if txn in seen:
                continue
            seen.add(txn)
        value = payload.get("value")
        amount = float(value) if isinstance(value, (int, float)) else 0.0
        out.append((dev, month_start(parse_dt(e["event_datetime"])), txn, amount))
    return out


def build_cohorts(first_installs: dict[str, dict], purchases: list[tuple],
                  max_life: int) -> list[tuple]:
    # device → (cohort_month, publisher); размер когорты.
    device_cohort: dict[str, tuple] = {}
    cohort_size: dict[tuple, int] = defaultdict(int)
    for dev, info in first_installs.items():
        cm = month_start(info["install_dt"])
        key = (cm, info["publisher"])
        device_cohort[dev] = key
        cohort_size[key] += 1

    # Покупатели, заказы и выручка считаются по-разному:
    #  • покупатель — устройство, засчитывается ОДИН раз, с месяца первой покупки;
    #  • заказ и выручка — каждая покупка отдельно, в свой месяц жизни.
    first_life: dict[str, int] = {}
    new_orders: dict[tuple, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    new_revenue: dict[tuple, dict[int, float]] = defaultdict(lambda: defaultdict(float))

    for dev, pmonth, _txn, amount in purchases:
        ck = device_cohort.get(dev)
        if not ck:
            continue
        lm = month_diff(pmonth, ck[0])
        if lm < 0 or lm > max_life:
            continue
        if dev not in first_life or lm < first_life[dev]:
            first_life[dev] = lm
        new_orders[ck][lm] += 1
        new_revenue[ck][lm] += amount

    # Новые покупатели по life_month на когорту. Устройство считается с life_month
    # его ПЕРВОЙ покупки. Покупка позже отчётного окна отфильтрована выше — иначе
    # она раздувала бы финальный кумулятивный столбец, по которому сверяем с UI AppMetrica.
    new_buyers: dict[tuple, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for dev, lm in first_life.items():
        new_buyers[device_cohort[dev]][lm] += 1

    out: list[tuple] = []
    for key, size in cohort_size.items():
        cm, pub = key
        buyers = orders = 0
        revenue = 0.0
        for lm in range(0, max_life + 1):
            buyers += new_buyers[key].get(lm, 0)
            orders += new_orders[key].get(lm, 0)
            revenue += new_revenue[key].get(lm, 0.0)
            out.append((cm, pub, lm, size, buyers, orders, round(revenue, 2)))
    return out


def sync_window(months: int, today: date) -> tuple[str, str]:
    first = date(today.year, today.month, 1)
    y, mo = first.year, first.month - (months - 1)
    while mo <= 0:
        mo += 12
        y -= 1
    since = date(y, mo, 1)
    return since.isoformat(), today.isoformat()


def month_chunks(since: str, until: str) -> list[tuple[str, str]]:
    """Разбить окно на календарные месяцы: [(YYYY-MM-DD, YYYY-MM-DD), ...].

    Нужно, чтобы не тянуть события с event_json за всё окно одним запросом.
    """
    start = datetime.strptime(since, "%Y-%m-%d").date()
    end = datetime.strptime(until, "%Y-%m-%d").date()
    out: list[tuple[str, str]] = []
    cur = date(start.year, start.month, 1)
    while cur <= end:
        nxt = date(cur.year + (cur.month // 12), (cur.month % 12) + 1, 1)
        out.append((max(cur, start).isoformat(), min(nxt - timedelta(days=1), end).isoformat()))
        cur = nxt
    return out


def _pg_url() -> str:
    return os.environ["DATABASE_URL"].split("?")[0]


def _write(installs_rows: list[tuple], cohort_rows: list[tuple]) -> None:
    conn = psycopg2.connect(_pg_url(), connect_timeout=30)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM lime_app_installs")
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO lime_app_installs (date, publisher, detail, campaign_id, installs) "
                "VALUES %s",
                installs_rows, page_size=500,
            )
            cur.execute("DELETE FROM lime_app_cohorts")
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO lime_app_cohorts "
                "(cohort_month, publisher, life_month, cohort_size, buyers, orders, revenue) "
                "VALUES %s",
                cohort_rows, page_size=500,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def sync_lime_appmetrica() -> None:
    token = os.environ.get("APPMETRICA_TOKEN")
    if not token:
        print("[lime-appmetrica] APPMETRICA_TOKEN не задан — пропуск")
        return
    app_id = os.environ.get("APPMETRICA_APP_ID") or "4415407"
    event_name = os.environ.get("APPMETRICA_EVENT_NAME") or "purchase"
    months = int(os.environ.get("APP_COHORT_MONTHS") or "7")
    max_life = int(os.environ.get("APP_MAX_LIFE") or "6")
    # Переатрибуции СЧИТАЕМ (дефолт "1"): AppMetrica их засчитывает как установки.
    # Откалибровано по слайдам — на промо-неделе 15.06 (14% недели = переатрибуции)
    # без них строка «Сайт-баннер» расходилась на -254, с ними на -1; по устоявшимся
    # неделям ошибка -0.30% -> +0.15%. Переустановки по-прежнему отбрасываем.
    keep_reattr = _truthy(os.environ.get("APP_KEEP_REATTR") or "1")
    keep_reinstall = _truthy(os.environ.get("APP_KEEP_REINSTALL") or "0")

    since, until = sync_window(months, date.today())
    print(f"[lime-appmetrica] окно {since}..{until}, app={app_id}, event={event_name}")

    installs_raw = fetch_installations(app_id, token, since, until)
    # События тянем ПОМЕСЯЧНО и сразу сворачиваем в факты: с event_json (там корзина)
    # всё окно одним куском — сотни мегабайт в памяти.
    purchases_raw: list[tuple] = []
    for chunk_since, chunk_until in month_chunks(since, until):
        chunk = fetch_purchase_events(app_id, token, chunk_since, chunk_until, event_name)
        purchases_raw.extend(purchase_facts(chunk))
        print(f"[lime-appmetrica] покупки {chunk_since}..{chunk_until}: "
              f"событий={len(chunk)}, фактов накоплено={len(purchases_raw)}", flush=True)
        del chunk
    print(f"[lime-appmetrica] сырьё: installs={len(installs_raw)}, purchases={len(purchases_raw)}")
    if not purchases_raw:
        print("[lime-appmetrica] WARNING: purchases_raw пуст — покупки не найдены за окно")

    first = first_install_per_device(installs_raw, keep_reattr, keep_reinstall)
    # Недельные установки — по сырым строкам (дедуп внутри недели); когорты — по первой
    # установке устройства. Это два разных вопроса и две разные агрегации.
    installs_rows = build_installs_daily(installs_raw, keep_reattr, keep_reinstall)
    cohort_rows = build_cohorts(first, purchases_raw, max_life)

    if not installs_rows:
        raise RuntimeError(
            "[lime-appmetrica] installs_rows пуст после агрегации — отказ от записи, "
            "чтобы не затереть данные витрины. Вероятные причины: пустой ответ Logs API "
            "(проверить APPMETRICA_TOKEN, APPMETRICA_APP_ID) или неверное окно дат "
            f"({since}..{until})."
        )

    _write(installs_rows, cohort_rows)
    print(f"[lime-appmetrica] записано: install-строк={len(installs_rows)}, "
          f"cohort-строк={len(cohort_rows)}")
