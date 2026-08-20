"""Yandex Cloud Search API (Wordstat) → lime_wordstat_demand (+ _daily).

Брендовый спрос (Σ 5 фраз, БЕЗ фильтра региона, широкое соответствие) в двух зернах:
недельном (вся история) и дневном (Wordstat хранит дневную детализацию только
за последние 60 дней — глубже дневного ряда не будет, это ок).
Старый api.wordstat.yandex.net закрыт — используем Search API.

Без региона — потому что так собирает ручной канон Павла (таблица «Частотность
брендовых запросов»): сверка 2026-08-20 по неделе 10.08 дала бит-в-бит совпадение
с его цифрами (Σ=183767), а прежний фильтр «Россия=225» давал стабильно −3%
(−5.3 тыс/нед), почти весь зазор — в латинской фразе «lime».

Auth: сервисный аккаунт (роль search-api.webSearch.user) → API-ключ.
Env: YANDEX_SEARCHAPI_KEY, YANDEX_CLOUD_FOLDER_ID, DATABASE_URL.
"""
import datetime as dt
import os

import requests

WORDSTAT_URL = "https://searchapi.api.cloud.yandex.net/v2/wordstat/dynamics"
BRAND_PHRASES = ["lime", "лайм интернет", "лайм купить", "лайм магазин", "лайм одежда"]
# Глубина дневной детализации Wordstat: «в дневном отображении данные показываются
# за последние 60 дней» (справка Wordstat). Граница у API СТРОГАЯ: from ровно 60 дней
# назад отвергается 400 «The from field value is older than 60 days» (проверено probe
# 2026-08-08), поэтому пол — 59 дней.
WORDSTAT_DAILY_DEPTH_DAYS = 59


def _monday(date_str: str) -> str:
    """ISO-понедельник недели для даты YYYY-MM-DD[...]. Единый ключ недели во всех рядах."""
    d = dt.date.fromisoformat(date_str[:10])
    return (d - dt.timedelta(days=d.weekday())).isoformat()


def _sunday(date_str: str) -> str:
    """ISO-воскресенье недели (конец недели) — граница toDate для PERIOD_WEEKLY."""
    d = dt.date.fromisoformat(date_str[:10])
    return (d + dt.timedelta(days=6 - d.weekday())).isoformat()


def last_closed_week_monday(today: dt.date | None = None) -> str:
    """ISO-понедельник ПОСЛЕДНЕЙ полностью закрытой недели (предыдущей от текущей)."""
    d = today or dt.date.today()
    cur_monday = d - dt.timedelta(days=d.weekday())
    return (cur_monday - dt.timedelta(days=7)).isoformat()


def daily_floor(today: dt.date | None = None) -> str:
    """Самая старая дата, за которую Wordstat ещё отдаёт дневную детализацию (60 дней назад).
    Пол запроса дневного синка: старше — API дневных точек не вернёт."""
    d = today or dt.date.today()
    return (d - dt.timedelta(days=WORDSTAT_DAILY_DEPTH_DAYS)).isoformat()


def daily_fresh_target(today: dt.date | None = None) -> str:
    """Дата, наличие которой в дневной таблице означает «спрос свеж» — вчера-1.
    Дневной Wordstat отстаёт на 1-3 дня, поэтому ждать «вчера» каждый прогон — значит
    дёргать API впустую; ориентир — позавчера (как last_closed_week_monday у недель)."""
    d = today or dt.date.today()
    return (d - dt.timedelta(days=2)).isoformat()


def daily_demand_up_to_date(table: str, region: str = "ru", today: dt.date | None = None) -> bool:
    """True, если в дневной таблице уже есть спрос за вчера-1 → синк можно пропустить.
    Как появился свежий день — крон отдыхает до следующего отставания.
    table — доверенный литерал (имя daily-таблицы), не пользовательский ввод."""
    from sync.db import get_connection

    target = daily_fresh_target(today)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT max(day) FROM {table} WHERE region = %s", (region,))
            row = cur.fetchone()
    mx = row[0] if row and row[0] else None
    return mx is not None and mx.isoformat() >= target


def demand_up_to_date(table: str, region: str = "ru", today: dt.date | None = None) -> bool:
    """True, если в demand-таблице уже есть спрос за последнюю ЗАКРЫТУЮ неделю → синк можно
    пропустить. Cloud Wordstat API отдаёт закрытую неделю с лагом ~1-2 нед; крон ежедневный
    дёргает API только пока прошлой недели нет, а как появилась — отдыхает до закрытия следующей.
    table — доверенный литерал (имя demand-таблицы), не пользовательский ввод."""
    from sync.db import get_connection

    target = last_closed_week_monday(today)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT max(week_start) FROM {table} WHERE region = %s", (region,))
            row = cur.fetchone()
    mx = row[0] if row and row[0] else None
    return mx is not None and mx.isoformat() >= target


def aggregate_weekly(responses: list[dict]) -> dict[str, int]:
    """Σ count по всем фразам, ключ = ISO-понедельник недели.

    responses — список ответов GetDynamics: {"results":[{"date","count","share"}]}.
    count приходит строкой (proto int64) → int().
    """
    out: dict[str, int] = {}
    for resp in responses:
        for pt in resp.get("results", []):
            wk = _monday(pt["date"])
            out[wk] = out.get(wk, 0) + int(pt.get("count", 0) or 0)
    return out


def _post_dynamics(body: dict) -> dict:
    """POST GetDynamics с авторизацией. Общий транспорт weekly- и daily-запросов."""
    api_key = os.environ["YANDEX_SEARCHAPI_KEY"]
    folder_id = os.environ.get("YANDEX_CLOUD_FOLDER_ID")  # опц.: ключ привязан к каталогу СА
    if folder_id:
        body["folderId"] = folder_id
    r = requests.post(
        WORDSTAT_URL, json=body, timeout=60,
        headers={"Authorization": f"Api-Key {api_key}", "Content-Type": "application/json"},
    )
    r.raise_for_status()
    return r.json()


def aggregate_daily(responses: list[dict]) -> dict[str, int]:
    """Σ count по всем фразам, ключ = день YYYY-MM-DD.

    responses — список ответов GetDynamics (PERIOD_DAILY): {"results":[{"date","count","share"}]}.
    count приходит строкой (proto int64) → int(); date режем до 10 символов —
    ключ дня стабилен и для YYYY-MM-DD, и для RFC3339-таймстампа.
    """
    out: dict[str, int] = {}
    for resp in responses:
        for pt in resp.get("results", []):
            day = pt["date"][:10]
            out[day] = out.get(day, 0) + int(pt.get("count", 0) or 0)
    return out


def fetch_phrase(phrase: str, from_date: str, to_date: str, regions: list[str] | None = None) -> dict:
    """GetDynamics по одной фразе за период (weekly). regions=None — без фильтра
    (все регионы, как в ручном каноне Павла — см. докстринг модуля)."""
    # API требует fromDate=понедельник, toDate=воскресенье (граница недели) для PERIOD_WEEKLY.
    body = {
        "phrase": phrase,
        "period": "PERIOD_WEEKLY",
        "fromDate": f"{_monday(from_date)}T00:00:00Z",
        "toDate": f"{_sunday(to_date)}T23:59:59Z",
    }
    if regions:
        body["regions"] = regions
    return _post_dynamics(body)


def fetch_phrase_daily(phrase: str, from_date: str, to_date: str, regions: list[str] | None = None) -> dict:
    """GetDynamics по одной фразе, дневная детализация (PERIOD_DAILY).

    В отличие от weekly края периода НЕ выравниваются: границу требует только
    weekly (toDate=воскресенье) и monthly (toDate=последний день месяца),
    дневные даты идут как есть. Глубже 60 дней API дневных точек не отдаёт.
    regions=None — без фильтра (та же методика, что у недельного ряда,
    иначе разъедется масштаб).
    """
    body = {
        "phrase": phrase,
        "period": "PERIOD_DAILY",
        "fromDate": f"{from_date[:10]}T00:00:00Z",
        "toDate": f"{to_date[:10]}T23:59:59Z",
    }
    if regions:
        body["regions"] = regions
    return _post_dynamics(body)


def sync_wordstat_demand(from_date: str, to_date: str) -> int:
    """Синк недельного спроса за период. Возвращает число записанных недель."""
    responses = [fetch_phrase(p, from_date, to_date) for p in BRAND_PHRASES]
    weekly = aggregate_weekly(responses)
    if not weekly:
        return 0
    from sync.db import get_connection  # ленивый импорт (psycopg2) — тесты чистых функций без БД

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO lime_wordstat_demand (week_start, region, frequency, updated_at)
                VALUES (%s, 'ru', %s, now())
                ON CONFLICT (week_start, region)
                DO UPDATE SET frequency = EXCLUDED.frequency, updated_at = now()
                """,
                [(wk, freq) for wk, freq in sorted(weekly.items())],
            )
        conn.commit()
    return len(weekly)


def sync_wordstat_demand_daily(from_date: str, to_date: str) -> int:
    """Синк ДНЕВНОГО спроса за период → lime_wordstat_demand_daily. Возвращает число дней.

    from клампится к daily_floor() (60-дневная глубина Wordstat), to — к сегодня:
    запрос старше/в будущее дневных точек не даст. Идемпотентно (upsert по (day, region)),
    поэтому бэкфилл и инкремент — один и тот же вызов на всё доступное окно.
    """
    today = dt.date.today()
    frm = max(from_date[:10], daily_floor(today))
    to = min(to_date[:10], today.isoformat())
    if frm > to:
        return 0
    responses = [fetch_phrase_daily(p, frm, to) for p in BRAND_PHRASES]
    daily = aggregate_daily(responses)
    if not daily:
        return 0
    from sync.db import get_connection  # ленивый импорт (psycopg2) — тесты чистых функций без БД

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO lime_wordstat_demand_daily (day, region, frequency, updated_at)
                VALUES (%s, 'ru', %s, now())
                ON CONFLICT (day, region)
                DO UPDATE SET frequency = EXCLUDED.frequency, updated_at = now()
                """,
                [(d, f) for d, f in sorted(daily.items())],
            )
        conn.commit()
    return len(daily)
