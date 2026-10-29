# -*- coding: utf-8 -*-
import csv
import gzip
import io
import json
import os
from datetime import date

import pytest

from sync import appmetrica_dsa as m

D = date.fromisoformat
H = 3600


def _csv(rows: list[dict]) -> str:
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=list(rows[0]) if rows else ["appmetrica_device_id"])
    w.writeheader()
    w.writerows(rows)
    return out.getvalue()


def _put(dtype: str, ts: int, rows: list[dict], upd: int | None = None):
    d = os.path.join(m.SPOOL, dtype)
    os.makedirs(d, exist_ok=True)
    with gzip.open(os.path.join(d, f"{ts}_{upd or ts + 400}.csv.gz"), "wt", encoding="utf-8") as fh:
        fh.write(_csv(rows))


@pytest.fixture(autouse=True)
def spool(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "SPOOL", str(tmp_path))
    return tmp_path


def test_covered_range_bootstrap_starts_at_status_and_stops_before_missing():
    assert m.covered_range([0, 300, 600, 900], {0, 300, 900}, None) == {"first": 0, "last": 300}
    assert m.covered_range([0, 300, 600], {0, 300, 600}, None) == {"first": 0, "last": 600}


def test_covered_range_keeps_first_when_old_windows_leave_status():
    prev = {"first": -3000, "last": 600}
    assert m.covered_range([300, 600, 900], {300, 600, 900}, prev) == {"first": -3000, "last": 900}


def test_covered_range_rewritten_window_shrinks_coverage_until_redownloaded():
    prev = {"first": 0, "last": 900}
    # окно 300 Яндекс дописал (новый update_timestamp), в спуле старая версия
    assert m.covered_range([0, 300, 600, 900], {0, 600, 900}, prev) == {"first": 0, "last": 0}


def test_covered_range_bootstrap_without_first_window_saves_nothing():
    assert m.covered_range([0, 300], {300}, None) is None


def test_complete_days_need_margin_on_both_sides():
    s = m.day_start(D("2026-10-23"))
    rng = {"first": s - m.MARGIN_SEC, "last": s + 86400 + m.MARGIN_SEC}
    assert m.complete_days(rng, D("2026-10-22"), D("2026-10-24")) == [D("2026-10-23")]
    rng["last"] -= 300
    assert m.complete_days(rng, D("2026-10-22"), D("2026-10-24")) == []


def test_day_start_is_moscow_midnight():
    assert m.day_start(D("2026-10-23")) == 1792702800  # 2026-10-22 21:00 UTC
    assert m.day_start(D("2026-10-24")) - m.day_start(D("2026-10-23")) == 86400


def test_collapse_installs_takes_last_version_and_drops_cancelled():
    rows = [
        {"installation_id": "a", "version": "1", "sign": "1", "tracker_name": "old"},
        {"installation_id": "a", "version": "2", "sign": "1", "tracker_name": "new"},
        {"installation_id": "b", "version": "1", "sign": "1"},
        {"installation_id": "b", "version": "2", "sign": "-1"},
    ]
    assert [r["tracker_name"] for r in m.collapse_installs(rows)] == ["new"]


def _feed(name):
    return next(f for f in m.FEEDS if f.stream == name)


def test_read_rows_purchases_by_receive_day_and_only_purchase_events():
    _put("event", 100, [
        {"event_name": "purchase", "appmetrica_device_id": "d1", "event_datetime": "2026-10-22 23:59:00",
         "event_receive_datetime": "2026-10-23 00:01:00"},
        {"event_name": "view_item", "appmetrica_device_id": "d2", "event_datetime": "2026-10-23 00:01:00",
         "event_receive_datetime": "2026-10-23 00:01:00"},
        {"event_name": "purchase", "appmetrica_device_id": "d3", "event_datetime": "2026-10-24 00:01:00",
         "event_receive_datetime": "2026-10-24 00:01:00"},
    ])
    out = m.read_rows(_feed("purchases"), {D("2026-10-23")}, 0)
    assert [r["appmetrica_device_id"] for r in out[D("2026-10-23")]] == ["d1"]
    assert D("2026-10-24") not in out


def test_read_rows_sessions_counted_per_device_day_across_windows():
    row = {"appmetrica_device_id": "d1", "session_start_datetime": "2026-10-23 10:00:00"}
    _put("session_start", 100, [row, row])
    _put("session_start", 400, [row, {"appmetrica_device_id": "d2",
                                       "session_start_datetime": "2026-10-23 11:00:00"}])
    out = m.read_rows(_feed("sessions"), {D("2026-10-23")}, 0)
    assert sorted((r["appmetrica_device_id"], r["n"]) for r in out[D("2026-10-23")]) == [("d1", 3), ("d2", 1)]


def test_read_rows_respects_window_bounds():
    row = {"appmetrica_device_id": "d1", "install_datetime": "2026-10-23 10:00:00", "installation_id": "i"}
    _put("installation", 100, [row])
    assert m.read_rows(_feed("installs"), {D("2026-10-23")}, 400) == {}


@pytest.fixture
def stored(monkeypatch):
    calls = []
    jobs = iter(range(1, 100))
    monkeypatch.setattr(m, "built_today", lambda conn, stream, a, b, today: set())
    monkeypatch.setattr(m, "new_job", lambda conn, stream, d, rng, today: next(jobs))
    monkeypatch.setattr(m.raw, "store", lambda conn, s, job, a, b, rows: calls.append((s.name, a, b, rows)))
    return calls


def test_build_writes_complete_days_through_raw_rows(stored):
    s23 = m.day_start(D("2026-10-23"))
    _put("event", s23 + H, [{"event_name": "purchase", "appmetrica_device_id": "d1",
                             "event_datetime": "2026-10-23 01:00:00", "event_receive_datetime": "2026-10-23 01:00:00",
                             "event_json": "{}", "city": "", "country_iso_code": "RU"}])
    full = {"first": s23 - H, "last": s23 + 86400 + H}
    n = m.build(None, {"event": full}, D("2026-10-24"))
    assert n == 1
    stream, a, b, rows = stored[0]
    assert (stream, a, b) == ("purchases", D("2026-10-23"), D("2026-10-23"))
    assert rows[0][:2] == (D("2026-10-23"), "d1") and rows[0][-1] == 1


def test_build_skips_days_built_today_and_incomplete(stored, monkeypatch):
    s22 = m.day_start(D("2026-10-22"))
    rng = {"first": s22 - H, "last": s22 + 2 * 86400 - H}  # 23.10 ещё не закрыт
    monkeypatch.setattr(m, "built_today", lambda conn, stream, a, b, today: {D("2026-10-22")})
    assert m.build(None, {"session_start": rng}, D("2026-10-24")) == 0
    assert stored == []


def test_build_late_install_lands_on_its_event_day(stored):
    s23 = m.day_start(D("2026-10-23"))
    # установка 23.10, пришла 25.10 — окно приёма за пределами суток 23.10
    _put("installation", s23 + 2 * 86400, [{
        "installation_id": "i1", "sign": "1", "version": "1", "appmetrica_device_id": "d1",
        "install_datetime": "2026-10-23 12:00:00", "install_receive_datetime": "2026-10-25 12:00:00",
        "publisher_name": "Yandex.Direct", "tracker_name": "t", "click_url_parameters": "",
        "is_reattribution": "false", "is_reinstallation": "false", "city": "", "country_iso_code": "RU"}])
    rng = {"first": s23 - H, "last": s23 + 3 * 86400}
    m.build(None, {"installation": rng}, D("2026-10-26"))
    by_day = {a: rows for stream, a, b, rows in stored if stream == "installs"}
    assert len(by_day[D("2026-10-23")]) == 1 and by_day[D("2026-10-24")] == []


def test_fetch_filters_events_redownloads_updated_and_waits_for_lag(monkeypatch, spool):
    now = 10_000
    status = {"status": {"streams": [
        {"data_type": "event", "stream_windows": [
            {"stream_window_timestamp": 300, "update_timestamp": 900},
            {"stream_window_timestamp": 600, "update_timestamp": now - 60},   # ещё дописывается
        ]},
        {"data_type": "installation", "stream_windows": [
            {"stream_window_timestamp": 300, "update_timestamp": 1200}]},
    ]}}
    _put("installation", 300, [{"appmetrica_device_id": "old"}], upd=700)
    got = []

    def fake_get(url, token):
        if url.endswith("/status"):
            return json.dumps(status).encode()
        got.append(url)
        if "data_type=event" in url:
            return _csv([{"event_name": "purchase", "x": "1"}, {"event_name": "screen", "x": "2"}]).encode()
        return _csv([{"appmetrica_device_id": "new"}]).encode()

    monkeypatch.setattr(m, "_get", fake_get)
    ranges = m.fetch("tok", now, deadline=float("inf"))
    assert len(got) == 2  # окно 600 не дописано — не качаем
    assert ranges["event"] == {"first": 300, "last": 300}
    inst = os.listdir(spool / "installation")
    assert "300_1200.csv.gz" in inst and "300_700.csv.gz" not in inst
    with gzip.open(spool / "event" / "300_900.csv.gz", "rt", encoding="utf-8") as fh:
        assert [r["event_name"] for r in csv.DictReader(fh)] == ["purchase"]
    assert json.load(open(spool / "event" / "_range.json")) == {"first": 300, "last": 300}


def test_prune_removes_windows_older_than_keep(spool):
    now = 100 * 86400
    _put("event", now - (m.KEEP_DAYS + 1) * 86400, [])
    _put("event", now - 86400, [])
    assert m.prune(now) == 1
    assert len([f for f in os.listdir(spool / "event") if f.endswith(".gz")]) == 1


def test_fetch_skips_window_expired_between_status_and_download(monkeypatch, spool):
    import requests

    status = {"status": {"streams": [{"data_type": "deeplink", "stream_windows": [
        {"stream_window_timestamp": 300, "update_timestamp": 900},
        {"stream_window_timestamp": 600, "update_timestamp": 1200}]}]}}

    def fake_get(url, token):
        if url.endswith("/status"):
            return json.dumps(status).encode()
        if "=300" in url:
            r = requests.Response()
            r.status_code = 404
            raise requests.HTTPError(response=r)
        return _csv([{"appmetrica_device_id": "d"}]).encode()

    monkeypatch.setattr(m, "_get", fake_get)
    ranges = m.fetch("tok", 10_000, deadline=float("inf"))
    assert os.listdir(spool / "deeplink") == ["600_1200.csv.gz"]
    assert ranges["deeplink"] is None  # первое окно потеряно — отрезок начнётся со следующего прогона


def test_covered_range_restarts_after_windows_expired_unseen():
    # спул кончился на 600, сборщик простоял: в status уже только окна с 3000
    prev = {"first": 0, "last": 600}
    assert m.covered_range([3000, 3300], {3000, 3300}, prev) == {"first": 3000, "last": 3300}


def test_covered_range_404_on_oldest_window_is_not_glued_next_run():
    prev = {"first": 0, "last": 900}
    # окно 300 отдало 404 и не скачано — отрезок кончается до него
    shrunk = m.covered_range([300, 600, 900], {600, 900}, prev)
    assert shrunk == {"first": 0, "last": 0}
    # следующий прогон: 300 из status ушло — дыру не склеиваем
    assert m.covered_range([600, 900], {600, 900}, shrunk) == {"first": 600, "last": 900}
