import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import datetime as dt

from sync.webmaster import (
    CLOSED_WEEK_MATURATION_DAYS,
    drop_leading_partial_week,
    drop_trailing_zero_days,
    merge_days,
    split_closed_week_rewrites,
    weekly_sums,
)


def test_merge_days_sums_hosts():
    a = {"2026-08-03": 100, "2026-08-04": 50}
    b = {"2026-08-03": 30, "2026-08-05": 7}
    assert merge_days([a, b]) == {"2026-08-03": 130, "2026-08-04": 50, "2026-08-05": 7}


def test_merge_days_normalizes_timestamp_keys():
    # ключ дня стабилен и для YYYY-MM-DD, и для RFC3339-таймстампа
    assert merge_days([{"2026-08-03T00:00:00Z": 5}, {"2026-08-03": 2}]) == {"2026-08-03": 7}


def test_weekly_sums_groups_by_iso_monday():
    days = {"2026-08-03": 10, "2026-08-09": 4, "2026-08-10": 7}  # Пн..Вс + след. Пн
    assert weekly_sums(days) == {"2026-08-03": 14, "2026-08-10": 7}


def test_drop_leading_partial_week_guards_api_depth_boundary():
    """Сводка отдаёт ~447 дней: самая старая неделя приходит без первых дней —
    её нельзя записывать усечённой (класс бага «граница окна», 2026-08-19).
    Текущая (правый край) остаётся: каждый прогон её дорисовывает."""
    days = {"2026-08-05": 3, "2026-08-06": 4, "2026-08-10": 9}  # ряд начался в среду
    weekly = weekly_sums(days)
    out = drop_leading_partial_week(weekly, days)
    assert "2026-08-03" not in out  # усечена слева → не трогаем сохранённое (file)
    assert out == {"2026-08-10": 9}


def test_drop_leading_partial_week_keeps_full_weeks():
    days = {"2026-08-03": 1, "2026-08-10": 2}  # ряд начался с понедельника
    weekly = weekly_sums(days)
    assert drop_leading_partial_week(weekly, days) == weekly
    assert drop_leading_partial_week({}, {}) == {}


def test_drop_trailing_zero_days_cuts_immature_tail_only():
    """Хвостовые нули = «ещё не собрано» (лаг ~2 дня) — срезаются;
    ноль в середине ряда — честный ноль, остаётся."""
    days = {"2026-08-15": 5, "2026-08-16": 0, "2026-08-17": 7,
            "2026-08-18": 0, "2026-08-19": 0}
    assert drop_trailing_zero_days(days) == {"2026-08-15": 5, "2026-08-16": 0, "2026-08-17": 7}
    assert drop_trailing_zero_days({}) == {}


# ── Гард закрытых недель ──────────────────────────────────────────────────────
# Синк переписывает всю историю каждый прогон — так недели дозревают. Цена того же
# механизма: усечённый ответ API молча затирает правильное значение старой недели,
# и заметить это нечем (updated_at у всех строк одинаковый, истории значений нет).

TODAY = dt.date(2026, 8, 27)
OPEN_WEEK = "2026-08-17"  # закончилась 23.08 — 4 дня назад, ещё дозревает
CLOSED_WEEK = "2026-06-01"  # закончилась 07.06 — далеко за окном дозревания


def test_open_week_is_rewritten_freely():
    """Ради этого синк и переписывает историю: Вебмастер доливает клики ~2 недели."""
    to_write, blocked = split_closed_week_rewrites(
        {OPEN_WEEK: (76833, 512791)}, {OPEN_WEEK: (70000, 500000)}, TODAY
    )
    assert to_write == {OPEN_WEEK: (76833, 512791)}
    assert blocked == []


def test_closed_week_rewrite_is_blocked_and_named():
    """Красный случай: дозревшая неделя приехала с другим значением."""
    to_write, blocked = split_closed_week_rewrites(
        {CLOSED_WEEK: (12000, 90000)}, {CLOSED_WEEK: (48452, 344563)}, TODAY
    )
    assert to_write == {}
    assert len(blocked) == 1
    assert blocked[0]["week_start"] == CLOSED_WEEK
    assert blocked[0]["stored"] == (48452, 344563)
    assert blocked[0]["incoming"] == (12000, 90000)
    assert blocked[0]["days_closed"] > CLOSED_WEEK_MATURATION_DAYS


def test_closed_week_unchanged_is_written_without_noise():
    """Совпало — не событие: обычный идемпотентный прогон."""
    to_write, blocked = split_closed_week_rewrites(
        {CLOSED_WEEK: (48452, 344563)}, {CLOSED_WEEK: (48452, 344563)}, TODAY
    )
    assert to_write == {CLOSED_WEEK: (48452, 344563)}
    assert blocked == []


def test_closed_week_tolerates_rounding():
    """Пересчёт на пол-процента — не подмена значения."""
    to_write, blocked = split_closed_week_rewrites(
        {CLOSED_WEEK: (48700, 344563)}, {CLOSED_WEEK: (48452, 344563)}, TODAY
    )
    assert blocked == []
    assert to_write == {CLOSED_WEEK: (48700, 344563)}


def test_missing_old_week_is_a_fill_not_a_rewrite():
    """Недели нет в базе — это заполнение пробела, писать можно даже старую."""
    to_write, blocked = split_closed_week_rewrites({CLOSED_WEEK: (48452, 344563)}, {}, TODAY)
    assert to_write == {CLOSED_WEEK: (48452, 344563)}
    assert blocked == []


def test_impressions_alone_can_trip_the_guard():
    """Клики совпали, показы переписаны вдвое — тоже правка истории."""
    _, blocked = split_closed_week_rewrites(
        {CLOSED_WEEK: (48452, 170000)}, {CLOSED_WEEK: (48452, 344563)}, TODAY
    )
    assert len(blocked) == 1


def test_guard_splits_batch_and_keeps_good_weeks():
    """Одна испорченная неделя не должна стоить остальным свежих данных."""
    to_write, blocked = split_closed_week_rewrites(
        {OPEN_WEEK: (76833, 512791), CLOSED_WEEK: (1, 1)},
        {OPEN_WEEK: (70000, 500000), CLOSED_WEEK: (48452, 344563)},
        TODAY,
    )
    assert list(to_write) == [OPEN_WEEK]
    assert [b["week_start"] for b in blocked] == [CLOSED_WEEK]
