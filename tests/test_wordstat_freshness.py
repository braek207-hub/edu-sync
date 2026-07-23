import datetime as dt

from sync.wordstat import last_closed_week_monday


def test_last_closed_week_monday_midweek():
    # Чт 2026-07-23 → текущая неделя пн 07-20 → последняя ЗАКРЫТАЯ неделя = пн 07-13.
    assert last_closed_week_monday(dt.date(2026, 7, 23)) == "2026-07-13"


def test_last_closed_week_monday_on_monday():
    # Пн 2026-07-20 (начало недели 30) → закрытая прошлая = 07-13.
    assert last_closed_week_monday(dt.date(2026, 7, 20)) == "2026-07-13"


def test_last_closed_week_monday_on_sunday():
    # Вс 2026-07-19 (конец недели 29, ещё её же неделя) → закрытая прошлая = 07-06.
    assert last_closed_week_monday(dt.date(2026, 7, 19)) == "2026-07-06"
