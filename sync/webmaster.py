"""Яндекс Вебмастер API → lime_brand_seo (+ _daily).

SEO-клики RU в двух зернах: недельном и дневном.

МЕТОДИКА = ручной лист Павла «Брендовые запросы LIMÉ (RU)», колонка SEO Yandex, 1-в-1
(решение 2026-08-21, сверено по живому API): сводный ряд ресурса
`search-queries/all/history` (TOTAL_CLICKS/TOTAL_SHOWS) — ВСЕ запросы, оба хоста,
БЕЗ бренд-фильтра. Его лист бьётся с этим рядом бит-в-бит на дозревших неделях
(06.10.25: 111 983 у API / 112 004 у листа), а недозревшие у него ниже на 0–11%:
Вебмастер доливает клики задним числом до ~2 недель.

Сводка = «качественный бренд» и по замеру (probe 2026-08-21, окно ~2 недели, оба
хоста): видимого небренда в кликах ~9%, и из него ~85–90% — ОПЕЧАТКИ бренда мимо
регекса написаний («дайм», «лацм», «лайи», «lame», «lume», «оайм») и артикулы;
настоящего небренда («магазин одежды», «белые джинсы») — ~1–2% кликов. Поэтому
небренд из сводки НЕ вычитаем (в отличие от GSC KZ/GCC): регекс вырезал бы
брендовые опечатки, а честная примесь и так в пределах шума.

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


# ─────────────────────────────────────────────────────────────────────────────
# Гард закрытых недель.
#
# Каждый прогон переписывает ВСЮ доступную историю (см. шапку модуля) — это и есть
# механизм дозревания: Вебмастер доливает клики задним числом до ~2 недель. Обратная
# сторона того же механизма: если API однажды вернёт по старой неделе усечённое
# значение, оно молча затрёт правильное, и заметить это будет нечем — updated_at
# у всех строк одинаковый, история значений не хранится нигде.
#
# Отсюда правило: неделя, закончившаяся достаточно давно, чтобы дозреть, больше
# не переписывается. Расхождение при перезаписи — не «свежие данные», а сигнал.
# ─────────────────────────────────────────────────────────────────────────────

# Три недели после конца недели: документированное дозревание ~2 недели, запас неделя.
CLOSED_WEEK_MATURATION_DAYS = 21
# Округления и мелкие пересчёты Вебмастера пропускаем, подмену значения — нет.
CLOSED_WEEK_TOLERANCE = 0.02


class ClosedWeekRewrite(Exception):
    """Источник попытался переписать уже дозревшую неделю другим значением."""


def _changed(old: int | None, new: int | None, tolerance: float) -> bool:
    if old is None or new is None:
        return old is not new
    if old == new:
        return False
    if old == 0:
        return new != 0
    return abs(new - old) / abs(old) > tolerance


def split_closed_week_rewrites(
    incoming: dict[str, tuple[int, int | None]],
    stored: dict[str, tuple[int, int | None]],
    today: dt.date,
    maturation_days: int = CLOSED_WEEK_MATURATION_DAYS,
    tolerance: float = CLOSED_WEEK_TOLERANCE,
) -> tuple[dict[str, tuple[int, int | None]], list[dict]]:
    """Разделить приехавшие недели на «писать» и «переписывание закрытой недели».

    Неделя, которой в базе ещё нет, пишется всегда — даже старая: это заполнение
    пробела, а не перезапись. Открытая (не дозревшая) неделя пишется всегда: ради
    этого синк и переписывает историю.
    """
    to_write: dict[str, tuple[int, int | None]] = {}
    blocked: list[dict] = []
    for wk, new in sorted(incoming.items()):
        old = stored.get(wk)
        if old is None:
            to_write[wk] = new
            continue
        week_end = dt.date.fromisoformat(wk) + dt.timedelta(days=6)
        if (today - week_end).days <= maturation_days:
            to_write[wk] = new
            continue
        if _changed(old[0], new[0], tolerance) or _changed(old[1], new[1], tolerance):
            blocked.append(
                {"week_start": wk, "stored": old, "incoming": new, "days_closed": (today - week_end).days}
            )
        else:
            to_write[wk] = new
    return to_write, blocked


def sync_brand_seo() -> int:
    """Синк недельных SEO-кликов (сводка, оба хоста, вся глубина API). Число недель."""
    clicks = _fetch_all("TOTAL_CLICKS")
    shows = _fetch_all("TOTAL_SHOWS")
    weekly_c = drop_leading_partial_week(weekly_sums(clicks), clicks)
    if not weekly_c:
        return 0
    weekly_s = weekly_sums(shows)
    incoming = {wk: (c, weekly_s.get(wk)) for wk, c in weekly_c.items()}
    from sync.db import get_connection  # ленивый импорт psycopg2

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT week_start::text, clicks, impressions FROM lime_brand_seo "
                "WHERE week_start = ANY(%s::date[])",
                (sorted(incoming),),
            )
            stored = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
            to_write, blocked = split_closed_week_rewrites(incoming, stored, dt.date.today())
            if to_write:
                cur.executemany(
                    """
                    INSERT INTO lime_brand_seo (week_start, clicks, impressions, source, updated_at)
                    VALUES (%s, %s, %s, 'webmaster', now())
                    ON CONFLICT (week_start)
                    DO UPDATE SET clicks = EXCLUDED.clicks, impressions = EXCLUDED.impressions,
                                  source = 'webmaster', updated_at = now()
                    """,
                    [(wk, c, s) for wk, (c, s) in sorted(to_write.items())],
                )
        conn.commit()

    if blocked:
        # Годные недели уже записаны — падаем после коммита, чтобы отказ от перезаписи
        # не стоил свежих данных. Прогон при этом краснеет: молчаливая правка истории
        # обязана быть видна.
        raise ClosedWeekRewrite(
            f"источник переписывает дозревшие недели ({len(blocked)}): {blocked[:5]}"
        )
    return len(to_write)


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
