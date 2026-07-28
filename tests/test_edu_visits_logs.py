from datetime import date, timedelta

from sync.edu_visits_logs import _chunk_windows, map_row


def test_map_row_selects_fields_and_buckets_phrase():
    header = ["ym:s:dateTime", "ym:s:clientID", "ym:s:visitID", "ym:s:lastDirectPhraseOrCond", "ym:s:isNewUser"]
    cols = ["2026-07-20 13:59:21", "123", "v1", "очень_редкая_фраза", "1"]
    row = map_row(header, cols, {"direct_phrase": set()})  # пустой allowed → phrase='other'
    assert row["client_id"] == "123" and row["visit_id"] == "v1"
    assert row["visit_ts"].startswith("2026-07-20 13:59:21")
    assert row["direct_phrase"] == "other"
    assert row["is_new_user"] == 1


def test_map_row_missing_fields_are_none_not_crash():
    header = ["ym:s:dateTime", "ym:s:clientID", "ym:s:visitID"]
    cols = ["2026-07-20 13:59:21", "123", "v1"]
    row = map_row(header, cols, {})
    assert row["utm_source"] is None
    assert row["visit_duration"] is None
    assert row["has_gclid"] is None


def test_map_row_never_includes_goal_fields():
    header = ["ym:s:dateTime", "ym:s:clientID", "ym:s:visitID"]
    cols = ["2026-07-20 13:59:21", "123", "v1"]
    row = map_row(header, cols, {})
    assert "goals_id" not in row
    assert not any("goal" in k.lower() for k in row.keys())


def test_chunk_windows_never_include_today():
    """Logs API 400 'date2 must be before today' — верхняя граница каждого окна
    должна быть строго до сегодня, иначе весь бэкфилл падает на последнем чанке."""
    today = date(2026, 7, 29)
    start = today - timedelta(days=10)
    windows = _chunk_windows(start, today, chunk_days=7)
    assert windows  # окна не пусты
    for date1, date2 in windows:
        assert date1 <= date2
        assert date2 < today.isoformat()


def test_chunk_windows_truncates_last_window_instead_of_including_today():
    """Окно, которое иначе включило бы сегодня частично (напр. вчера..сегодня) —
    обрезается до вчера, не пропускается целиком (данные за вчера не теряем)."""
    today = date(2026, 7, 29)
    start = today - timedelta(days=8)
    windows = _chunk_windows(start, today, chunk_days=7)
    assert windows[-1] == ((today - timedelta(days=1)).isoformat(), (today - timedelta(days=1)).isoformat())


def test_chunk_windows_skips_window_that_is_only_today():
    """Чанк, который целиком состоит из сегодня (start=end=today) — не должен создавать
    logrequest вовсе (пустое окно после клампа), а не упасть с 400."""
    today = date(2026, 7, 29)
    windows = _chunk_windows(today, today, chunk_days=7)
    assert windows == []
