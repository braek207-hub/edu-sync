import datetime as dt

from sync.wordstat import daily_floor, daily_fresh_target, last_closed_week_monday


def test_last_closed_week_monday_midweek():
    # Чт 2026-07-23 → текущая неделя пн 07-20 → последняя ЗАКРЫТАЯ неделя = пн 07-13.
    assert last_closed_week_monday(dt.date(2026, 7, 23)) == "2026-07-13"


def test_last_closed_week_monday_on_monday():
    # Пн 2026-07-20 (начало недели 30) → закрытая прошлая = 07-13.
    assert last_closed_week_monday(dt.date(2026, 7, 20)) == "2026-07-13"


def test_last_closed_week_monday_on_sunday():
    # Вс 2026-07-19 (конец недели 29, ещё её же неделя) → закрытая прошлая = 07-06.
    assert last_closed_week_monday(dt.date(2026, 7, 19)) == "2026-07-06"


def test_daily_floor_is_59_days_back():
    # Глубина дневной детализации Wordstat — 60 дней, но граница у API строгая:
    # from ровно 60 дней назад отвергается 400 → пол 59 дней (probe 2026-08-08).
    assert daily_floor(dt.date(2026, 8, 8)) == "2026-06-10"


def test_daily_fresh_target_is_two_days_back():
    # Свежесть дневного ряда меряем по «вчера-1» (лаг Wordstat 1-3 дня).
    assert daily_fresh_target(dt.date(2026, 8, 8)) == "2026-08-06"
