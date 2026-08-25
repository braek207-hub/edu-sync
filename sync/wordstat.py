"""Yandex Cloud Search API (Wordstat) → lime_wordstat_demand (+ _daily).

Брендовый спрос (Σ фраз региона, широкое соответствие) в двух зернах: недельном
(вся история) и дневном (Wordstat хранит дневную детализацию только за последние
60 дней — глубже дневного ряда не будет, это ок).
Старый api.wordstat.yandex.net закрыт — используем Search API.

Регионы: ru (без гео-фильтра — канон Павла), kz и gcc (гео-фильтр + локальные
бренд-фразы, см. REGION_GEO / REGION_EXTRA_PHRASES). Ряд региона лежит в тех же
таблицах — region входит в первичный ключ.

Без региона — потому что так собирает ручной канон Павла (таблица «Частотность
брендовых запросов»): сверка 2026-08-20 по неделе 10.08 дала бит-в-бит совпадение
с его цифрами (Σ=183767), а прежний фильтр «Россия=225» давал стабильно −3%
(−5.3 тыс/нед), почти весь зазор — в латинской фразе «lime».

Auth: сервисный аккаунт (роль search-api.webSearch.user) → API-ключ.
Env: YANDEX_SEARCHAPI_KEY, YANDEX_CLOUD_FOLDER_ID, DATABASE_URL.
"""
import datetime as dt
import os
import time

import requests

WORDSTAT_URL = "https://searchapi.api.cloud.yandex.net/v2/wordstat/dynamics"
BRAND_PHRASES = ["lime", "лайм интернет", "лайм купить", "лайм магазин", "лайм одежда"]

# Гео-фильтр региона спроса: id из справочника GeoRegions Директа (сверено 2026-08-25).
# ru — None: без фильтра, так собирает ручной канон Павла (см. докстринг модуля).
# gcc — 6 стран Залива одним списком: API суммирует список регионов (проба 2026-08-25,
# неделя 10.08 по фразе «lime»: ОАЭ отдельно 107, шестёрка списком 111 — добавка мелких стран).
REGION_GEO: dict[str, list[str] | None] = {
    "ru": None,
    "kz": ["159"],  # Казахстан
    "gcc": [
        "210",    # Объединённые Арабские Эмираты
        "10540",  # Саудовская Аравия
        "21486",  # Катар
        "10537",  # Кувейт
        "10532",  # Бахрейн
        "21586",  # Оман
    ],
}

# Региональные добавки к бренд-набору. Широкое соответствие «lime» уже ловит любые
# запросы со словом lime («lime kz» 2089/мес, «lime dubai» 98/мес), поэтому добираем
# только то, что мимо него: кириллицу с локальными уточнениями и слитное «limestore».
# Замеры topRequests+regions 2026-08-25 (показов/мес): KZ +774 к базовым 6326 (+12%),
# GCC +33 к 504 (+7%). «лайм официальный» (KZ 111) НЕ берём — на 90% это «лайм
# официальный сайт», уже покрытый фразой «лайм сайт»: был бы двойной счёт.
# Арабица («لايم», «ليم») и «leem» в Wordstat по Заливу дают ноль — не берём.
REGION_EXTRA_PHRASES: dict[str, list[str]] = {
    "kz": [
        "лайм кз",         # 366
        "лайм сайт",       # 153
        "лайм казахстан",  # 76
        "лайм алматы",     # 52
        "limestore",       # 55
        "лайм каталог",    # 37
        "лайм астана",     # 35
    ],
    "gcc": [
        "лайм дубай",    # 15
        "лайм сайт",     # 12
        "limestore",     # 4
        "лайм каталог",  # 2
    ],
}


def phrases_for(region: str) -> list[str]:
    """Бренд-набор региона: канон RU + локальные написания."""
    return BRAND_PHRASES + REGION_EXTRA_PHRASES.get(region, [])


def geo_for(region: str) -> list[str] | None:
    """Гео-фильтр региона (None = без фильтра). Неизвестный регион — KeyError, а не тихий тотал."""
    return REGION_GEO[region]
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
    """POST GetDynamics с авторизацией. Общий транспорт weekly- и daily-запросов.

    Ретрай с бэкоффом на 429 (rate limit Search API): объединённый ecom-синк дёргает
    много фраз подряд по нескольким проектам и упирается в лимит — без ретрая наборы
    падали целиком (последний, meshnflesh, страдал больше всех). Уважаем Retry-After,
    иначе экспонента 2^n (кап 30с), до 6 попыток.
    """
    api_key = os.environ["YANDEX_SEARCHAPI_KEY"]
    folder_id = os.environ.get("YANDEX_CLOUD_FOLDER_ID")  # опц.: ключ привязан к каталогу СА
    if folder_id:
        body["folderId"] = folder_id
    headers = {"Authorization": f"Api-Key {api_key}", "Content-Type": "application/json"}
    for attempt in range(6):
        r = requests.post(WORDSTAT_URL, json=body, timeout=60, headers=headers)
        if r.status_code == 429 and attempt < 5:
            wait = float(r.headers.get("Retry-After") or 0) or min(2**attempt, 30)
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()  # недостижимо: цикл всегда вернул или бросил — для полноты типов
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


def sync_wordstat_demand(from_date: str, to_date: str, region: str = "ru") -> int:
    """Синк недельного спроса региона за период. Возвращает число записанных недель."""
    geo = geo_for(region)
    responses = [fetch_phrase(p, from_date, to_date, geo) for p in phrases_for(region)]
    weekly = aggregate_weekly(responses)
    if not weekly:
        return 0
    from sync.db import get_connection  # ленивый импорт (psycopg2) — тесты чистых функций без БД

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO lime_wordstat_demand (week_start, region, frequency, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (week_start, region)
                DO UPDATE SET frequency = EXCLUDED.frequency, updated_at = now()
                """,
                [(wk, region, freq) for wk, freq in sorted(weekly.items())],
            )
        conn.commit()
    return len(weekly)


def sync_wordstat_demand_daily(from_date: str, to_date: str, region: str = "ru") -> int:
    """Синк ДНЕВНОГО спроса региона за период → lime_wordstat_demand_daily. Число дней.

    from клампится к daily_floor() (60-дневная глубина Wordstat), to — к сегодня:
    запрос старше/в будущее дневных точек не даст. Идемпотентно (upsert по (day, region)),
    поэтому бэкфилл и инкремент — один и тот же вызов на всё доступное окно.
    """
    today = dt.date.today()
    frm = max(from_date[:10], daily_floor(today))
    to = min(to_date[:10], today.isoformat())
    if frm > to:
        return 0
    geo = geo_for(region)
    responses = [fetch_phrase_daily(p, frm, to, geo) for p in phrases_for(region)]
    daily = aggregate_daily(responses)
    if not daily:
        return 0
    from sync.db import get_connection  # ленивый импорт (psycopg2) — тесты чистых функций без БД

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO lime_wordstat_demand_daily (day, region, frequency, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (day, region)
                DO UPDATE SET frequency = EXCLUDED.frequency, updated_at = now()
                """,
                [(d, region, f) for d, f in sorted(daily.items())],
            )
        conn.commit()
    return len(daily)
