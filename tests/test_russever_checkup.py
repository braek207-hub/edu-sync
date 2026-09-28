# -*- coding: utf-8 -*-
"""Арифметика ежедневного чекапа РосСеверЭкспо и остановки города в 18:00."""
import datetime as dt

from sync.russever import checkup, close_city
from sync.russever.cities import BUDGET, CITY, ROLE_SHARE, TZ


def test_week_start_is_monday():
    assert checkup.week_start(dt.date(2026, 9, 28)) == dt.date(2026, 9, 28)
    assert checkup.week_start(dt.date(2026, 10, 4)) == dt.date(2026, 9, 28)


def test_plan_of_falls_back_to_last_known_week():
    monday = dt.date(2026, 9, 21)
    assert checkup.plan_of("chelyabinsk", monday) == BUDGET["chelyabinsk"]["2026-09-21"]
    # Недели после таблицы наследуют последнюю заданную, а не обнуляются.
    assert checkup.plan_of("chelyabinsk", dt.date(2026, 10, 12)) > 0
    assert checkup.plan_of("magadan", monday) == 0


def test_role_shares_push_video_into_corridor():
    base = ROLE_SHARE["Видео"]
    low = checkup.role_shares({"video_share": 25.0})
    high = checkup.role_shares({"video_share": 70.0})
    inside = checkup.role_shares({"video_share": 45.0})
    assert low["Видео"] > base and low["Баннеры"] < ROLE_SHARE["Баннеры"]
    assert high["Видео"] < base and high["Баннеры"] > ROLE_SHARE["Баннеры"]
    assert inside == ROLE_SHARE
    for shares in (low, high, inside):
        assert abs(sum(shares.values()) - sum(ROLE_SHARE.values())) < 1e-9


def test_every_budget_city_has_timezone_and_dates():
    for slug in BUDGET:
        assert slug in CITY, slug
        assert slug in TZ, slug


def _utc(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text).replace(tzinfo=dt.timezone.utc)


def test_stop_only_after_local_six_pm_of_last_day():
    # Мирный (UTC+9) закрывается 04.10: в 09:00 UTC там ровно 18:00.
    assert not close_city.should_stop("mirny", _utc("2026-10-04T08:59"))
    assert close_city.should_stop("mirny", _utc("2026-10-04T09:00"))
    # Сургут (UTC+5) в тот же день ещё работает до 13:00 UTC.
    assert not close_city.should_stop("surgut", _utc("2026-10-04T09:00"))
    assert close_city.should_stop("surgut", _utc("2026-10-04T13:00"))


def test_stop_stays_true_after_the_closing_day():
    assert close_city.should_stop("mirny", _utc("2026-10-15T03:00"))


def test_watch_counts_minutes_only_on_the_closing_day():
    assert close_city.minutes_left("surgut", _utc("2026-10-04T12:30")) == 30
    assert close_city.minutes_left("surgut", _utc("2026-10-04T13:00")) is None
    # Не день закрытия — дежурить незачем.
    assert close_city.minutes_left("chelyabinsk", _utc("2026-10-04T08:00")) is None
