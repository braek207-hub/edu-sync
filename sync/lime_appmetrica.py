"""LIME · AppMetrica: агрегация сырья Logs API в витрины дашборда.

installs → недельные установки по партнёру (уникальные устройства, первая установка).
cohorts  → устройства-покупатели по когорте (месяц × партнёр) накопительно по месяцам жизни.
Сырьё не хранится — только агрегаты. Границы недели/месяца — по времени установки/события.
"""
import os
from collections import defaultdict
from datetime import date, datetime

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


def build_installs_weekly(installs: list[dict], keep_reattribution: bool,
                          keep_reinstall: bool) -> list[tuple]:
    """Установки недели = уникальные устройства, установившие ИМЕННО в эту неделю.

    Дедуп локальный, внутри (неделя × партнёр), а НЕ по первой установке за всё окно:
    иначе устройство, поставившее приложение повторно, выпадало бы из свежих недель
    (сверка со слайдами: глобальный дедуп давал −606 на неделе 29.06, локальный +206).
    Когорты — наоборот, живут на первой установке (см. build_cohorts).
    """
    seen: dict[tuple, set] = defaultdict(set)
    for r in installs:
        if not keep_reinstall and _truthy(r.get("is_reinstallation")):
            continue
        if not keep_reattribution and _truthy(r.get("is_reattribution")):
            continue
        dev = r.get("appmetrica_device_id")
        if not dev:
            continue
        wk = iso_monday(parse_dt(r["install_datetime"]))
        seen[(wk, r.get("publisher_name") or "unknown")].add(dev)
    return [(w, p, len(devs)) for (w, p), devs in seen.items()]


def build_cohorts(first_installs: dict[str, dict], purchases: list[dict],
                  max_life: int) -> list[tuple]:
    # device → (cohort_month, publisher); размер когорты.
    device_cohort: dict[str, tuple] = {}
    cohort_size: dict[tuple, int] = defaultdict(int)
    for dev, info in first_installs.items():
        cm = month_start(info["install_dt"])
        key = (cm, info["publisher"])
        device_cohort[dev] = key
        cohort_size[key] += 1

    # device → life_month его ПЕРВОЙ покупки (>=0).
    first_life: dict[str, int] = {}
    for p in purchases:
        dev = p.get("appmetrica_device_id")
        ck = device_cohort.get(dev)
        if not ck:
            continue
        lm = month_diff(month_start(parse_dt(p["event_datetime"])), ck[0])
        if lm < 0:
            continue
        if dev not in first_life or lm < first_life[dev]:
            first_life[dev] = lm

    # новые покупатели по life_month на когорту.
    # Устройство считается с life_month его ПЕРВОЙ покупки. Если первая покупка
    # позже отчётного окна (lm > max_life) — устройство ещё не покупало ни в
    # одном отчётном месяце, поэтому исключаем его, а не клэмпим в max_life
    # (клэмп раздувал бы финальный кумулятивный столбец, по которому сверяем с UI AppMetrica).
    new_buyers: dict[tuple, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for dev, lm in first_life.items():
        if lm > max_life:
            continue
        new_buyers[device_cohort[dev]][lm] += 1

    out: list[tuple] = []
    for key, size in cohort_size.items():
        cm, pub = key
        cumulative = 0
        for lm in range(0, max_life + 1):
            cumulative += new_buyers[key].get(lm, 0)
            out.append((cm, pub, lm, size, cumulative))
    return out


def sync_window(months: int, today: date) -> tuple[str, str]:
    first = date(today.year, today.month, 1)
    y, mo = first.year, first.month - (months - 1)
    while mo <= 0:
        mo += 12
        y -= 1
    since = date(y, mo, 1)
    return since.isoformat(), today.isoformat()


def _pg_url() -> str:
    return os.environ["DATABASE_URL"].split("?")[0]


def _write(installs_rows: list[tuple], cohort_rows: list[tuple]) -> None:
    conn = psycopg2.connect(_pg_url(), connect_timeout=30)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM lime_app_installs")
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO lime_app_installs (week, publisher, installs) VALUES %s",
                installs_rows, page_size=500,
            )
            cur.execute("DELETE FROM lime_app_cohorts")
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO lime_app_cohorts "
                "(cohort_month, publisher, life_month, cohort_size, buyers) VALUES %s",
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
    keep_reattr = _truthy(os.environ.get("APP_KEEP_REATTR") or "0")
    keep_reinstall = _truthy(os.environ.get("APP_KEEP_REINSTALL") or "0")

    since, until = sync_window(months, date.today())
    print(f"[lime-appmetrica] окно {since}..{until}, app={app_id}, event={event_name}")

    installs_raw = fetch_installations(app_id, token, since, until)
    purchases_raw = fetch_purchase_events(app_id, token, since, until, event_name)
    print(f"[lime-appmetrica] сырьё: installs={len(installs_raw)}, purchases={len(purchases_raw)}")
    if not purchases_raw:
        print("[lime-appmetrica] WARNING: purchases_raw пуст — покупки не найдены за окно")

    first = first_install_per_device(installs_raw, keep_reattr, keep_reinstall)
    # Недельные установки — по сырым строкам (дедуп внутри недели); когорты — по первой
    # установке устройства. Это два разных вопроса и две разные агрегации.
    installs_rows = build_installs_weekly(installs_raw, keep_reattr, keep_reinstall)
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
