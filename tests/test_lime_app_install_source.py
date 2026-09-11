"""Сессии/покупки приложения по дате × кампания установки (sync/lime_app_install_source)."""
from datetime import datetime

from sync.lime_app_install_source import COLUMNS, build_source_daily, day_chunks

FIRST = {
    # device → (install_dt, publisher, detail, campaign_id) — форма first_install_attribution
    "d1": (datetime(2026, 7, 1), "VK Ads (ex. myTarget)", "", "2780001"),
    "d2": (datetime(2026, 7, 2), "VK Ads (ex. myTarget)", "", "2780001"),
    "d3": (datetime(2026, 8, 1), "Yandex.Direct", "", "117845740"),
    "d4": (datetime(2026, 8, 1), "unknown", "", ""),
}


def _by_key(rows):
    return {(r[0].isoformat(), r[1], r[2]): dict(zip(COLUMNS[3:], r[3:])) for r in rows}


def test_sessions_grouped_by_event_date_and_install_campaign():
    sessions = [
        {"appmetrica_device_id": "d1", "session_start_datetime": "2026-09-01 10:00:00"},
        {"appmetrica_device_id": "d1", "session_start_datetime": "2026-09-01 18:00:00"},
        {"appmetrica_device_id": "d2", "session_start_datetime": "2026-09-01 12:00:00"},
        {"appmetrica_device_id": "d3", "session_start_datetime": "2026-09-02 12:00:00"},
    ]
    rows = _by_key(build_source_daily(FIRST, sessions, []))
    vk = rows[("2026-09-01", "VK Ads (ex. myTarget)", "2780001")]
    assert vk["sessions"] == 3 and vk["devices"] == 2
    assert rows[("2026-09-02", "Yandex.Direct", "117845740")]["sessions"] == 1


def test_purchases_land_on_purchase_date_not_install_date():
    purchases = [("d3", datetime(2026, 9, 5, 9, 0), "t1", 3500.0),
                 ("d3", datetime(2026, 9, 5, 21, 0), "t2", 1500.0)]
    rows = _by_key(build_source_daily(FIRST, [], purchases))
    r = rows[("2026-09-05", "Yandex.Direct", "117845740")]
    assert r["orders"] == 2 and r["revenue"] == 5000.0 and r["sessions"] == 0


def test_device_without_install_in_window_is_unknown_remainder():
    sessions = [{"appmetrica_device_id": "old", "session_start_datetime": "2026-09-01 10:00:00"},
                {"appmetrica_device_id": "d4", "session_start_datetime": "2026-09-01 10:00:00"}]
    rows = _by_key(build_source_daily(FIRST, sessions, []))
    assert rows[("2026-09-01", "unknown", "")]["sessions"] == 2


def test_day_chunks_cover_window_without_gaps():
    assert day_chunks("2026-09-01", "2026-09-07", 3) == [
        ("2026-09-01", "2026-09-03"), ("2026-09-04", "2026-09-06"), ("2026-09-07", "2026-09-07")]
