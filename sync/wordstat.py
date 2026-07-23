"""Yandex Cloud Search API (Wordstat) → lime_wordstat_demand.

Недельный брендовый спрос (Σ 5 фраз, регион Россия=225, широкое соответствие).
Старый api.wordstat.yandex.net закрыт — используем Search API.

Auth: сервисный аккаунт (роль search-api.webSearch.user) → API-ключ.
Env: YANDEX_SEARCHAPI_KEY, YANDEX_CLOUD_FOLDER_ID, DATABASE_URL.
"""
import datetime as dt
import os

import requests

WORDSTAT_URL = "https://searchapi.api.cloud.yandex.net/v2/wordstat/dynamics"
RUSSIA_REGION = "225"  # регион Wordstat «Россия»
BRAND_PHRASES = ["lime", "лайм интернет", "лайм купить", "лайм магазин", "лайм одежда"]


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


def fetch_phrase(phrase: str, from_date: str, to_date: str, regions: list[str] | None = None) -> dict:
    """GetDynamics по одной фразе за период (weekly). regions — список region-id (дефолт РФ)."""
    api_key = os.environ["YANDEX_SEARCHAPI_KEY"]
    folder_id = os.environ.get("YANDEX_CLOUD_FOLDER_ID")  # опц.: ключ привязан к каталогу СА
    # API требует fromDate=понедельник, toDate=воскресенье (граница недели) для PERIOD_WEEKLY.
    body = {
        "phrase": phrase,
        "period": "PERIOD_WEEKLY",
        "fromDate": f"{_monday(from_date)}T00:00:00Z",
        "toDate": f"{_sunday(to_date)}T23:59:59Z",
        "regions": regions or [RUSSIA_REGION],
    }
    if folder_id:
        body["folderId"] = folder_id
    r = requests.post(
        WORDSTAT_URL, json=body, timeout=60,
        headers={"Authorization": f"Api-Key {api_key}", "Content-Type": "application/json"},
    )
    r.raise_for_status()
    return r.json()


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
