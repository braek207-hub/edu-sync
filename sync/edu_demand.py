"""EDU «Спрос рынка»: недельный рыночный спрос из Wordstat, по-фразно → edu_wordstat_demand.

Переиспользует сбор из sync/wordstat.py (fetch_phrase). В отличие от LIME (Σ 5 брендовых
фраз в одну строку) — храним КАЖДУЮ фразу отдельно: Σ по неделе считается на чтении, состав
фраз можно менять без ре-бэкфилла. Крупные непересекающиеся корни, регион Россия (225).

Env: YANDEX_SEARCHAPI_KEY, DATABASE_URL.
"""
import datetime as dt

from sync.wordstat import daily_floor, fetch_phrase, fetch_phrase_daily, _monday

MSK_REGION_ID = "1"  # «Москва и область» (совпадает с CRM msk_mo); проверено probe в Step 1

# Крупные корни без вложенности: широкое соответствие корня уже включает вложенные
# запросы («поступить в колледж», «колледж заочно»), их не добавляем — иначе Σ двоит.
# Уровневые корни без вложенности. Маппинг фраза→сегмент — в дашборде (lib/dashboard/demand-traffic.ts).
EDU_DEMAND_PHRASES: list[str] = [
    # СПО
    "колледж", "техникум", "училище", "ссуз", "среднее профессиональное",
    # Высшее (ВПО)
    "вуз", "университет", "институт", "высшее образование", "бакалавриат", "специалитет",
    # Магистратура / Аспирантура (отдельные сегменты)
    "магистратура", "аспирантура",
    # Дистант / Заочно
    "дистанционное обучение", "дистанционное образование", "заочное обучение",
    # ДПО (широкая «переподготовка» вместо узкой «профессиональная переподготовка»)
    "переподготовка", "повышение квалификации", "профпереподготовка",
]

EDU_DEMAND_REGIONS: list[tuple[str, list[str]]] = [
    ("ru", ["225"]),
    ("msk", [MSK_REGION_ID]),
]


def aggregate_weekly_by_phrase(phrase: str, resp: dict) -> dict[str, int]:
    """{ISO-Пн → count} для одной фразы. count приходит строкой (proto int64) → int()."""
    out: dict[str, int] = {}
    for pt in resp.get("results", []):
        wk = _monday(pt["date"])
        out[wk] = out.get(wk, 0) + int(pt.get("count", 0) or 0)
    return out


def aggregate_daily_by_phrase(resp: dict) -> dict[str, int]:
    """{день YYYY-MM-DD → count} для одной фразы (ответ PERIOD_DAILY). count приходит
    строкой (proto int64) → int(); date режем до 10 символов — ключ дня стабилен
    и для YYYY-MM-DD, и для RFC3339-таймстампа."""
    out: dict[str, int] = {}
    for pt in resp.get("results", []):
        day = pt["date"][:10]
        out[day] = out.get(day, 0) + int(pt.get("count", 0) or 0)
    return out


def sync_edu_wordstat_demand(from_date: str, to_date: str) -> int:
    """Синк спроса по всем EDU-фразам за период × два региона (ru, msk). Строки = week×phrase×region."""
    rows: list[tuple[str, str, str, int]] = []  # (week_start, region, phrase, frequency)
    for region_key, region_ids in EDU_DEMAND_REGIONS:
        for phrase in EDU_DEMAND_PHRASES:
            weekly = aggregate_weekly_by_phrase(
                phrase, fetch_phrase(phrase, from_date, to_date, regions=region_ids)
            )
            rows.extend((wk, region_key, phrase, freq) for wk, freq in weekly.items())
    if not rows:
        return 0

    from sync.db import get_connection  # ленивый импорт (psycopg2) — тесты чистых функций без БД

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO edu_wordstat_demand (week_start, region, phrase, frequency, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (week_start, region, phrase)
                DO UPDATE SET frequency = EXCLUDED.frequency, updated_at = now()
                """,
                [(wk, region_key, phrase, freq) for wk, region_key, phrase, freq in sorted(rows)],
            )
        conn.commit()
    return len(rows)


def sync_edu_wordstat_demand_daily(from_date: str, to_date: str) -> int:
    """Синк ДНЕВНОГО спроса по всем EDU-фразам × два региона → edu_wordstat_demand_daily.

    from клампится к daily_floor() (60-дневная глубина Wordstat), to — к сегодня:
    запрос старше/в будущее дневных точек не даст. Идемпотентно (upsert по
    (day, region, phrase)) — бэкфилл и инкремент один и тот же вызов на всё окно.
    """
    today = dt.date.today()
    frm = max(from_date[:10], daily_floor(today))
    to = min(to_date[:10], today.isoformat())
    if frm > to:
        return 0
    rows: list[tuple[str, str, str, int]] = []  # (day, region, phrase, frequency)
    for region_key, region_ids in EDU_DEMAND_REGIONS:
        for phrase in EDU_DEMAND_PHRASES:
            daily = aggregate_daily_by_phrase(
                fetch_phrase_daily(phrase, frm, to, regions=region_ids)
            )
            rows.extend((d, region_key, phrase, freq) for d, freq in daily.items())
    if not rows:
        return 0

    from sync.db import get_connection  # ленивый импорт (psycopg2) — тесты чистых функций без БД

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO edu_wordstat_demand_daily (day, region, phrase, frequency, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (day, region, phrase)
                DO UPDATE SET frequency = EXCLUDED.frequency, updated_at = now()
                """,
                sorted(rows),
            )
        conn.commit()
    return len(rows)