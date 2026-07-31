# -*- coding: utf-8 -*-
"""AppMetrica GCC: классификация партнёров, last-touch атрибуция, агрегация."""
from sync.gcc_app import aggregate, app_bucket, attribute, build_touches


def test_app_bucket_paid_vs_organic():
    assert app_bucket("Google Ads") == "app_paid"
    assert app_bucket("Yandex.Direct Auto-Tracking") == "app_paid"
    assert app_bucket("Instagram") == "app_paid"
    assert app_bucket("VK Ads (ex. myTarget)") == "app_paid"
    assert app_bucket("Mindbox") == "app_org"
    assert app_bucket("") == "app_org"
    assert app_bucket(None) == "app_org"
    assert app_bucket("Website") == "app_org"


def test_attribute_last_touch_prefers_recent_deeplink():
    touches = build_touches(
        installs=[{"appmetrica_device_id": "d1", "install_datetime": "2026-06-01 10:00:00",
                   "publisher_name": ""}],  # органическая установка
        deeplinks=[{"appmetrica_device_id": "d1", "event_datetime": "2026-07-20 09:00:00",
                    "publisher_name": "Google Ads"}],  # платный ре-энгейджмент
    )
    # событие ПОСЛЕ диплинка → берём диплинк (Google Ads)
    assert attribute("d1", "2026-07-25 12:00:00", touches) == "Google Ads"
    # событие ДО диплинка → падаем на установку (органика)
    assert attribute("d1", "2026-06-15 12:00:00", touches) == ""
    # неизвестное устройство → пусто
    assert attribute("d404", "2026-07-25 12:00:00", touches) == ""


def test_aggregate_filters_gcc_and_dedups_orders():
    touches = build_touches(
        installs=[
            {"appmetrica_device_id": "d1", "install_datetime": "2026-07-01 00:00:00",
             "publisher_name": "Google Ads"},   # paid
            {"appmetrica_device_id": "d2", "install_datetime": "2026-07-01 00:00:00",
             "publisher_name": ""},              # organic
        ],
        deeplinks=[],
    )
    sessions = [
        {"appmetrica_device_id": "d1", "session_start_datetime": "2026-07-28 08:00:00",
         "country_iso_code": "AE"},   # UAE, paid
        {"appmetrica_device_id": "d2", "session_start_datetime": "2026-07-28 09:00:00",
         "country_iso_code": "AE"},   # UAE, organic
        {"appmetrica_device_id": "d2", "session_start_datetime": "2026-07-28 10:00:00",
         "country_iso_code": "RU"},   # не Залив → отброшено
    ]
    purchases = [
        {"appmetrica_device_id": "d1", "event_datetime": "2026-07-28 08:30:00",
         "country_iso_code": "AE", "event_json": '{"transaction_id": "T1", "value": 100}'},
        {"appmetrica_device_id": "d1", "event_datetime": "2026-07-28 08:31:00",
         "country_iso_code": "AE", "event_json": '{"transaction_id": "T1", "value": 100}'},  # дубль
        {"appmetrica_device_id": "d2", "event_datetime": "2026-07-28 11:00:00",
         "country_iso_code": "SA", "event_json": '{"transaction_id": "T2", "value": 50}'},
    ]
    traffic, orders = aggregate(sessions, purchases, touches, ["2026-07-28"])
    t = traffic["2026-07-28"]
    assert t["UAE"]["app_paid"] == 1     # d1
    assert t["UAE"]["app_org"] == 1      # d2
    assert t["GCC"]["app_paid"] == 1 and t["GCC"]["app_org"] == 1  # RU не в счёте
    o = orders["2026-07-28"]
    assert o["UAE"]["app_paid"] == 1     # T1 один раз (дедуп)
    assert o["KSA"]["app_org"] == 1      # T2, d2 organic
    assert o["GCC"]["app_paid"] == 1 and o["GCC"]["app_org"] == 1


def test_aggregate_traffic_is_dau_unique_devices():
    """Трафик = DAU: несколько сессий одного устройства за день = 1, не число сессий."""
    touches = build_touches(
        installs=[{"appmetrica_device_id": "d1", "install_datetime": "2026-07-01 00:00:00",
                   "publisher_name": ""}],  # organic
        deeplinks=[],
    )
    sessions = [
        {"appmetrica_device_id": "d1", "session_start_datetime": f"2026-07-28 0{h}:00:00",
         "country_iso_code": "AE"} for h in range(1, 6)  # 5 сессий одного устройства
    ]
    traffic, _ = aggregate(sessions, [], touches, ["2026-07-28"])
    assert traffic["2026-07-28"]["UAE"]["app_org"] == 1     # DAU=1, не 5
    assert traffic["2026-07-28"]["GCC"]["app_org"] == 1


def test_fetch_app_traffic_chunks_and_merges(monkeypatch):
    """Сессии тянутся чанками, per-day DAU мержится по дням без дедупа между чанками."""
    from datetime import date, timedelta

    import sync.appmetrica_logs as logs
    from sync.gcc_app import fetch_app_traffic

    # касания: одно paid-устройство d1 (installations), диплинков нет
    monkeypatch.setattr(logs, "fetch_export", lambda ep, *a, **k: (
        [{"appmetrica_device_id": "d1", "install_datetime": "2026-06-01 00:00:00",
          "publisher_name": "Google Ads"}] if ep == "installations" else []))

    calls = []

    def fake_sessions(app_id, token, ds, du, country=False):
        calls.append((ds, du))
        out, dd = [], date.fromisoformat(ds)
        while dd <= date.fromisoformat(du):  # d1 активен каждый день окна
            out.append({"appmetrica_device_id": "d1",
                        "session_start_datetime": f"{dd} 08:00:00", "country_iso_code": "AE"})
            dd += timedelta(days=1)
        return out

    monkeypatch.setattr(logs, "fetch_sessions", fake_sessions)

    dates = ["2026-07-01", "2026-07-02", "2026-07-03"]
    tr = fetch_app_traffic("t", "app", dates, lookback_days=30, chunk_days=2)
    assert len(calls) == 2  # [01-02] и [03]
    for iso in dates:
        assert tr[iso]["UAE"]["app_paid"] == 1
        assert tr[iso]["GCC"]["app_paid"] == 1


def test_aggregate_empty():
    traffic, orders = aggregate([], [], {}, ["2026-07-28"])
    assert traffic["2026-07-28"]["GCC"]["app_org"] == 0
    assert orders["2026-07-28"]["UAE"]["app_paid"] == 0
