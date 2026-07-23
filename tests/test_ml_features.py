from datetime import date
from sync.ml.features import days_to_deadline

DEADLINES = [date(2025, 8, 20), date(2026, 8, 20)]

def test_days_to_deadline_upcoming():
    assert days_to_deadline(date(2026, 8, 10), DEADLINES) == 10

def test_days_to_deadline_picks_nearest_future():
    assert days_to_deadline(date(2025, 8, 21), DEADLINES) == 364  # до 2026-08-20

def test_days_to_deadline_all_past_returns_negative():
    assert days_to_deadline(date(2026, 8, 25), DEADLINES) == -5   # 5 дней после последнего
