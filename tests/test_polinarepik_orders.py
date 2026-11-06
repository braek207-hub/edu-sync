from datetime import date, timedelta

import pytest

from sync import polinarepik_orders as po


def test_window_default_last_days_without_date_to():
    assert po.window_params(None, None, date(2026, 11, 6)) == {
        "date_from": (date(2026, 11, 6) - timedelta(days=po.WINDOW_DAYS)).isoformat()
    }


def test_window_explicit_range_wins():
    assert po.window_params("2026-03-01", "2026-03-11", date(2026, 11, 6)) == {
        "date_from": "2026-03-01",
        "date_to": "2026-03-11",
    }


def test_window_rejects_bad_date():
    with pytest.raises(ValueError):
        po.window_params("2026-13-01", None, date(2026, 11, 6))
