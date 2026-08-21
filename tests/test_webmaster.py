import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.webmaster import (
    drop_leading_partial_week,
    drop_trailing_zero_days,
    merge_days,
    seo_daily_fresh_target,
    weekly_sums,
)
import datetime as dt


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


def test_seo_daily_fresh_target_is_two_days_lag():
    assert seo_daily_fresh_target(dt.date(2026, 8, 21)) == "2026-08-18"
