"""BJORN «Спрос рынка»: недельный категорийный спрос из Wordstat, по-фразно → bjorn_wordstat_demand.

Зеркало sync/edu_demand.py. Переиспользует fetch_phrase (Wordstat Search API) и агрегатор недель.
Корни — по типам верхней одежды каталога Bjorn Larsen (куртки/парки/аляски/пуховики), регион Россия.
Каждую фразу храним отдельно → Σ по неделе считается на чтении, состав фраз можно менять без ре-бэкфилла.

Env: YANDEX_SEARCHAPI_KEY, DATABASE_URL.
"""
from sync.edu_demand import aggregate_weekly_by_phrase
from sync.wordstat import fetch_phrase

# Крупные корни по категориям (без бренда/моделей и без вложенности): бомбер/парка/пуховик —
# непересекающиеся типы; «куртка мужская/женская/зимняя/демисезонная» — разбиение категории
# «куртка» на непересекающиеся срезы (голую «куртка» не берём — она поглотила бы остальные и двоила Σ).
BJORN_DEMAND_PHRASES: list[str] = [
    "куртка мужская",
    "куртка женская",
    "зимняя куртка",
    "демисезонная куртка",
    "парка",
    "куртка аляска",
    "пуховик",
    "бомбер",
    "ветровка",
    "косуха",
]


def sync_bjorn_wordstat_demand(from_date: str, to_date: str) -> int:
    """Синк спроса по всем BJORN-фразам за период. Возвращает число строк (week×phrase)."""
    rows: list[tuple[str, str, int]] = []  # (week_start, phrase, frequency)
    for phrase in BJORN_DEMAND_PHRASES:
        weekly = aggregate_weekly_by_phrase(phrase, fetch_phrase(phrase, from_date, to_date))
        rows.extend((wk, phrase, freq) for wk, freq in weekly.items())
    if not rows:
        return 0

    from sync.db import get_connection  # ленивый импорт (psycopg2) — тесты чистых функций без БД

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO bjorn_wordstat_demand (week_start, region, phrase, frequency, updated_at)
                VALUES (%s, 'ru', %s, %s, now())
                ON CONFLICT (week_start, region, phrase)
                DO UPDATE SET frequency = EXCLUDED.frequency, updated_at = now()
                """,
                [(wk, phrase, freq) for wk, phrase, freq in sorted(rows)],
            )
        conn.commit()
    return len(rows)
