"""Обобщённый спрос Wordstat для e-com дашбордов → общая таблица ecom_wordstat_demand[_daily].

Зеркало sync/bjorn_demand.py, но параметризовано по (slug, kind): один код для нишевого и
брендового спроса любого e-com проекта. Пофразное хранение (Σ на чтении) — состав фраз меняется
без ре-бэкфилла. Клики/покупки в дашборде общие, здесь их нет — только линия спроса.

Env: YANDEX_SEARCHAPI_KEY, DATABASE_URL.
"""
import datetime as dt

from sync.bjorn_demand import BJORN_DEMAND_PHRASES
from sync.edu_demand import aggregate_daily_by_phrase, aggregate_weekly_by_phrase
from sync.wordstat import daily_floor, fetch_phrase, fetch_phrase_daily

# Брендовые написания + опечатки вычитаны Павлом (спека 2026-08-17). Нишевый bjorn —
# переиспользуем BJORN_DEMAND_PHRASES (единый источник, без копипаста).
ECOM_PHRASE_SETS: dict[str, dict[str, list[str]]] = {
    "bjorn": {
        "niche": BJORN_DEMAND_PHRASES,
        "brand": [
            "бьорн", "bjorn", "бьёрн", "бйорн", "бъёрн", "беорн", "biorn", "byorn",
            "бьерн", "бёрн", "бьорн куртка", "bjorn куртка", "бьорн куртки",
            "бьорн одежда", "бьорн магазин", "бьорн пуховик",
        ],
    },
    "polinarepik": {
        "brand": [
            "полина репик", "polina repik", "полинарепик", "polinarepik",
            "репик полина", "полина репек", "полина рэпик", "polina repik одежда",
            "полина репик магазин", "polina repik бренд", "репик",
        ],
    },
    "meshnflesh": {
        "brand": [
            "mesh n flesh", "меш н флеш", "mesh and flesh", "meshnflesh",
            "меш флеш", "mesh flesh", "меш энд флеш", "мэш н флэш",
            "мэш флэш", "меш н флэш",
        ],
    },
}


def sync_ecom_wordstat_demand(
    slug: str, kind: str, phrases: list[str], from_date: str, to_date: str, region: str = "ru"
) -> int:
    """Недельный спрос набора (slug, kind) за период → ecom_wordstat_demand. Число строк week×phrase."""
    rows: list[tuple[str, str, int]] = []  # (week_start, phrase, frequency)
    for phrase in phrases:
        weekly = aggregate_weekly_by_phrase(phrase, fetch_phrase(phrase, from_date, to_date))
        rows.extend((wk, phrase, freq) for wk, freq in weekly.items())
    if not rows:
        return 0
    from sync.db import get_connection  # ленивый импорт (psycopg2) — тесты чистых функций без БД

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO ecom_wordstat_demand (slug, kind, region, phrase, week_start, frequency, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (slug, kind, region, phrase, week_start)
                DO UPDATE SET frequency = EXCLUDED.frequency, updated_at = now()
                """,
                [(slug, kind, region, phrase, wk, freq) for wk, phrase, freq in sorted(rows)],
            )
        conn.commit()
    return len(rows)


def sync_ecom_wordstat_demand_daily(
    slug: str, kind: str, phrases: list[str], from_date: str, to_date: str, region: str = "ru"
) -> int:
    """Дневной спрос набора (slug, kind) → ecom_wordstat_demand_daily. from клампится к daily_floor()."""
    today = dt.date.today()
    frm = max(from_date[:10], daily_floor(today))
    to = min(to_date[:10], today.isoformat())
    if frm > to:
        return 0
    rows: list[tuple[str, str, int]] = []  # (day, phrase, frequency)
    for phrase in phrases:
        daily = aggregate_daily_by_phrase(fetch_phrase_daily(phrase, frm, to))
        rows.extend((d, phrase, freq) for d, freq in daily.items())
    if not rows:
        return 0
    from sync.db import get_connection  # ленивый импорт (psycopg2) — тесты чистых функций без БД

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO ecom_wordstat_demand_daily (slug, kind, region, phrase, day, frequency, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (slug, kind, region, phrase, day)
                DO UPDATE SET frequency = EXCLUDED.frequency, updated_at = now()
                """,
                # SQL-колонки: (slug, kind, region, phrase, day, frequency) — порядок значений
                # должен зеркалить это, а не порядок сборки rows (day, phrase, freq).
                [(slug, kind, region, phrase, day, freq) for day, phrase, freq in sorted(rows)],
            )
        conn.commit()
    return len(rows)


def ecom_demand_up_to_date(slug: str, kind: str, region: str = "ru", today: dt.date | None = None) -> bool:
    """True, если последняя закрытая неделя набора уже в таблице → недельный синк можно пропустить."""
    from sync.db import get_connection
    from sync.wordstat import last_closed_week_monday

    target = last_closed_week_monday(today)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT max(week_start) FROM ecom_wordstat_demand WHERE slug=%s AND kind=%s AND region=%s",
                (slug, kind, region),
            )
            row = cur.fetchone()
    mx = row[0] if row and row[0] else None
    return mx is not None and mx.isoformat() >= target


def ecom_daily_demand_up_to_date(slug: str, kind: str, region: str = "ru", today: dt.date | None = None) -> bool:
    """True, если свежий день набора уже в дневной таблице → дневной синк можно пропустить."""
    from sync.db import get_connection
    from sync.wordstat import daily_fresh_target

    target = daily_fresh_target(today)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT max(day) FROM ecom_wordstat_demand_daily WHERE slug=%s AND kind=%s AND region=%s",
                (slug, kind, region),
            )
            row = cur.fetchone()
    mx = row[0] if row and row[0] else None
    return mx is not None and mx.isoformat() >= target
