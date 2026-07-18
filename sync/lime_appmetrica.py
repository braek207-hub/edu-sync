"""LIME · AppMetrica: агрегация сырья Logs API в витрины дашборда.

installs → недельные установки по партнёру (уникальные устройства, первая установка).
cohorts  → устройства-покупатели по когорте (месяц × партнёр) накопительно по месяцам жизни.
Сырьё не хранится — только агрегаты. Границы недели/месяца — по времени установки/события.
"""
from collections import defaultdict
from datetime import date, datetime


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


def build_installs_weekly(first_installs: dict[str, dict]) -> list[tuple]:
    agg: dict[tuple, int] = defaultdict(int)
    for info in first_installs.values():
        agg[(iso_monday(info["install_dt"]), info["publisher"])] += 1
    return [(w, p, n) for (w, p), n in agg.items()]


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
