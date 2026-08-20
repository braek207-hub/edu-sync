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


def test_fetch_app_dau_total_parses_and_sums_gcc(monkeypatch):
    """Reporting total: маппинг стран (рус.→код), GCC = сумма 5, чужие страны отброшены."""
    import json as _json
    from contextlib import contextmanager

    import sync.gcc_app as ga

    payload = {"data": [
        {"dimensions": [{"name": "2026-07-28"}, {"name": "Объединённые Арабские Эмираты"}],
         "metrics": [179.0]},
        {"dimensions": [{"name": "2026-07-28"}, {"name": "Саудовская Аравия"}], "metrics": [40.0]},
        {"dimensions": [{"name": "2026-07-28"}, {"name": "Россия"}], "metrics": [196.0]},  # не Залив
    ]}

    @contextmanager
    def fake_urlopen(req, timeout=90):
        class R:
            def read(self):
                return _json.dumps(payload).encode("utf-8")
        yield R()

    monkeypatch.setattr(ga.urllib.request, "urlopen", fake_urlopen)
    out = ga.fetch_app_dau_total("t", "app", ["2026-07-28"])
    assert out["2026-07-28"]["UAE"] == 179
    assert out["2026-07-28"]["KSA"] == 40
    assert out["2026-07-28"]["GCC"] == 219   # 179+40, Россия не в счёте
    assert "QA" in out["2026-07-28"] and out["2026-07-28"]["QA"] == 0


def test_aggregate_empty():
    traffic, orders = aggregate([], [], {}, ["2026-07-28"])
    assert traffic["2026-07-28"]["GCC"]["app_org"] == 0
    assert orders["2026-07-28"]["UAE"]["app_paid"] == 0


# === App-трафик для дашборда (lime_stats data_source='app') ===

from sync.gcc_app import build_app_traffic_rows  # noqa: E402
from sync.gcc_channels import map_app_publisher  # noqa: E402


def test_map_app_publisher_paid_networks():
    assert map_app_publisher("Google Ads") == ("SEM", "Google.Adwords", "Платный")
    assert map_app_publisher("Instagram") == ("SMM paid", "Meta Ads", "Платный")
    assert map_app_publisher("Facebook Ads") == ("SMM paid", "Meta Ads", "Платный")
    assert map_app_publisher("TikTok For Business") == ("SMM paid", "TikTok Ads", "Платный")


def test_map_app_publisher_empty_is_direct():
    """Нет касаний = открыл приложение сам → Direct, как прямые заходы web."""
    assert map_app_publisher("") == ("Direct", "Direct", "Бесплатный")
    assert map_app_publisher(None) == ("Direct", "Direct", "Бесплатный")


def test_map_app_publisher_unknown_partner_is_referral():
    ch, sub, tt = map_app_publisher("Website")
    assert ch == "Referrals" and sub == "Website" and tt == "Бесплатный"


def test_build_app_traffic_rows_residual_in_direct():
    """Тотал страны = Reporting; размеченные каналы из Logs; остаток — в Direct."""
    total = {"2026-08-10": {"UAE": 100}}
    channels = {"2026-08-10": {"ОАЭ": {
        ("SEM", "Google.Adwords", "Платный"): [30, 45],
        ("Direct", "Direct", "Бесплатный"): [50, 80],   # Logs-цифра Direct отбрасывается
    }}}
    rows = build_app_traffic_rows(total, channels, ["2026-08-10"])
    by_ch = {r["subchannel"]: r for r in rows}
    assert by_ch["Google.Adwords"]["users"] == 30 and by_ch["Google.Adwords"]["sessions"] == 45
    # Direct = 100 − 30 (остаток к Reporting-тоталу), не 50 из Logs
    assert by_ch["Direct"]["users"] == 70
    assert sum(r["users"] for r in rows) == 100


def test_build_app_traffic_rows_reporting_only():
    """Logs упал → весь тотал страны одной строкой Direct."""
    total = {"2026-08-10": {"KSA": 40}}
    rows = build_app_traffic_rows(total, {}, ["2026-08-10"])
    assert len(rows) == 1
    r = rows[0]
    assert r["country"] == "Саудовская Аравия" and r["channel"] == "Direct" and r["users"] == 40


def test_build_app_traffic_rows_clamps_negative_residual():
    """Logs-каналы больше Reporting-тотала (пересечение уников) → Direct не уходит в минус."""
    total = {"2026-08-10": {"QA": 20}}
    channels = {"2026-08-10": {"Катар": {("SEM", "Google.Adwords", "Платный"): [25, 30]}}}
    rows = build_app_traffic_rows(total, channels, ["2026-08-10"])
    assert all(r["users"] >= 0 for r in rows)
    assert not any(r["channel"] == "Direct" for r in rows)  # остатка нет


def test_app_traffic_tuples_column_positions():
    from sync.lime_gcc import COLUMNS, app_traffic_tuples
    rows = [{"date": "2026-08-10", "country": "ОАЭ", "channel": "SEM",
             "subchannel": "Google.Adwords", "traffic_type": "Платный",
             "users": 30, "sessions": 45}]
    by_day = app_traffic_tuples(rows)
    (t,) = by_day["2026-08-10"]
    assert len(t) == len(COLUMNS)
    row = dict(zip(COLUMNS, t))
    assert row["data_source"] == "app" and row["users"] == 30 and row["sessions"] == 45
    assert row["purchases_count"] == 0 and row["cost"] == 0.0
