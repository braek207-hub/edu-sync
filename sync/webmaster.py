"""Яндекс Вебмастер API → lime_brand_seo (+ _daily).

SEO-клики RU в двух зернах: недельном и дневном.

МЕТОДИКА = ручной лист Павла «Брендовые запросы LIMÉ (RU)», колонка SEO Yandex, 1-в-1
(решение 2026-08-21, сверено по живому API): сводный ряд ресурса
`search-queries/all/history` (TOTAL_CLICKS/TOTAL_SHOWS) — ВСЕ запросы, оба хоста,
БЕЗ бренд-фильтра. Его лист бьётся с этим рядом бит-в-бит на дозревших неделях
(06.10.25: 111 983 у API / 112 004 у листа), а недозревшие у него ниже на 0–11%:
Вебмастер доливает клики задним числом до ~2 недель.

Почему НЕ query-analytics (прежний путь, бренд-фильтр): его окно ~2 недели и сотни
тысяч строк пагинации; сводка отдаёт ~15 месяцев (447 точек) двумя GET-запросами,
поэтому каждый прогон переписывает ВСЮ доступную историю — закрытые недели дозревают
у нас автоматически, гард от усечения нужен только на левой границе глубины API.
Бренд-срез при нужде — в git-истории до 2026-08-21.

История старше глубины API (2023 — май-2025) остаётся source='file' из листа Павла —
его колонка всегда была этим же сводным рядом, методика ряда единая.

Env: WORDSTAT_WEBMASTER_TOKEN, DATABASE_URL.
"""
import datetime as dt
import os

import requests

WM_BASE = "https://api.webmaster.yandex.net/v4"
USER_ID = "1343007866"
HOSTS = ["https:limestore.com:443", "https:lime-shop.com:443"]


def _monday(date_str: str) -> str:
    d = dt.date.fromisoformat(date_str[:10])
    return (d - dt.timedelta(days=d.weekday())).isoformat()


def fetch_total_history(host_id: str, indicator: str, date_from: str, date_to: str) -> dict[str, int]:
    """Сводный ряд ресурса по дням → {day: value}. indicator: TOTAL_CLICKS | TOTAL_SHOWS."""
    token = os.environ["WORDSTAT_WEBMASTER_TOKEN"]
    r = requests.get(
        f"{WM_BASE}/user/{USER_ID}/hosts/{host_id}/search-queries/all/history",
        params={"query_indicator": indicator, "date_from": date_from, "date_to": date_to},
        headers={"Authorization": f"OAuth {token}"},
        timeout=60,
    )
    r.raise_for_status()
    out: dict[str, int] = {}
    for p in r.json().get("indicators", {}).get(indicator, []):
        out[p["date"][:10]] = int(p.get("value", 0) or 0)
    return out


def merge_days(per_host: list[dict[str, int]]) -> dict[str, int]:
    """Суммирует дневные ряды хостов: {day: Σ value}."""
    out: dict[str, int] = {}
    for series in per_host:
        for day, v in series.items():
            out[out_key(day)] = out.get(out_key(day), 0) + v
    return out


def out_key(day: str) -> str:
    """Ключ дня стабилен и для YYYY-MM-DD, и для RFC3339-таймстампа."""
    return day[:10]


def weekly_sums(days: dict[str, int]) -> dict[str, int]:
    """{day: value} → {monday: Σ value}."""
    out: dict[str, int] = {}
    for day, v in days.items():
        wk = _monday(day)
        out[wk] = out.get(wk, 0) + v
    return out


def drop_leading_partial_week(weekly: dict[str, int], days: dict[str, int]) -> dict[str, int]:
    """Убрать неделю, начавшуюся раньше глубины API, — её сумма усечена слева.

    Сводка отдаёт ~447 дней; самая старая неделя приходит без своих первых дней,
    и upsert записал бы её усечённой (класс бага «граница окна», см. 2026-08-19).
    Правый край (текущая неделя) остаётся — каждый прогон её дорисовывает."""
    if not days:
        return weekly
    min_day = min(days)
    return {wk: v for wk, v in weekly.items() if wk >= min_day}


def drop_trailing_zero_days(days: dict[str, int]) -> dict[str, int]:
    """Срезать нулевой хвост ряда: сводка отдаёт сегодняшний/вчерашний день нулём,
    пока статистика не доехала (лаг ~2 дня). Ноль в середине ряда — честный ноль,
    хвостовой ноль — «ещё не собрано»: записав его, нарисуем ложный обвал."""
    keys = sorted(days)
    while keys and days[keys[-1]] == 0:
        keys.pop()
    return {k: days[k] for k in keys}


def _fetch_all(indicator: str) -> dict[str, int]:
    """Σ по хостам за всю глубину API (клампится самим API), без нулевого хвоста."""
    frm = (dt.date.today() - dt.timedelta(days=460)).isoformat()
    to = dt.date.today().isoformat()
    return drop_trailing_zero_days(
        merge_days([fetch_total_history(h, indicator, frm, to) for h in HOSTS])
    )


def sync_brand_seo() -> int:
    """Синк недельных SEO-кликов (сводка, оба хоста, вся глубина API). Число недель."""
    clicks = _fetch_all("TOTAL_CLICKS")
    shows = _fetch_all("TOTAL_SHOWS")
    weekly_c = drop_leading_partial_week(weekly_sums(clicks), clicks)
    if not weekly_c:
        return 0
    weekly_s = weekly_sums(shows)
    from sync.db import get_connection  # ленивый импорт psycopg2

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO lime_brand_seo (week_start, clicks, impressions, source, updated_at)
                VALUES (%s, %s, %s, 'webmaster', now())
                ON CONFLICT (week_start)
                DO UPDATE SET clicks = EXCLUDED.clicks, impressions = EXCLUDED.impressions,
                              source = 'webmaster', updated_at = now()
                """,
                [(wk, c, weekly_s.get(wk)) for wk, c in sorted(weekly_c.items())],
            )
        conn.commit()
    return len(weekly_c)


def sync_brand_seo_daily() -> int:
    """Синк ДНЕВНЫХ SEO-кликов → lime_brand_seo_daily. Возвращает число дней.

    Тот же сводный ряд, что у недель (иначе зерна разъедутся); вся глубина API,
    идемпотентный upsert по day — свежие дни дозаливаются при каждом прогоне."""
    clicks = _fetch_all("TOTAL_CLICKS")
    if not clicks:
        return 0
    shows = _fetch_all("TOTAL_SHOWS")
    from sync.db import get_connection  # ленивый импорт psycopg2

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO lime_brand_seo_daily (day, clicks, impressions, source, updated_at)
                VALUES (%s, %s, %s, 'webmaster', now())
                ON CONFLICT (day)
                DO UPDATE SET clicks = EXCLUDED.clicks, impressions = EXCLUDED.impressions,
                              source = 'webmaster', updated_at = now()
                """,
                [(d, c, shows.get(d)) for d, c in sorted(clicks.items())],
            )
        conn.commit()
    return len(clicks)
