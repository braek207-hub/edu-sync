# -*- coding: utf-8 -*-
from datetime import date, datetime, timedelta

import pytest

from sync import appmetrica_raw as r


D = date.fromisoformat


def test_chunks_month_and_week_are_clipped():
    assert r.chunks(D("2024-08-15"), D("2024-10-03"), "month") == [
        (D("2024-08-15"), D("2024-08-31")), (D("2024-09-01"), D("2024-09-30")),
        (D("2024-10-01"), D("2024-10-03"))]
    # 2026-09-15 — вторник: первая неделя обрезана слева, нарезка с понедельника
    assert r.chunks(D("2026-09-15"), D("2026-09-28"), "week") == [
        (D("2026-09-15"), D("2026-09-20")), (D("2026-09-21"), D("2026-09-27")),
        (D("2026-09-28"), D("2026-09-28"))]


def _days(a, b):
    return {D(a) + timedelta(days=i) for i in range((D(b) - D(a)).days + 1)}


def test_missing_ranges_skip_covered_and_narrow_to_gaps():
    covered = _days("2024-08-01", "2024-08-31") | {D("2024-09-01"), D("2024-09-02"), D("2024-09-30")}
    assert r.missing_ranges(covered, D("2024-08-01"), D("2024-09-30"), "month") == [
        (D("2024-09-03"), D("2024-09-29"))]


def test_plan_tail_first_then_backfill_newest_first_without_tail_days():
    jobs = r.plan(D("2026-10-25"), {})
    tails = [j for j in jobs if j["kind"] == "tail"]
    assert [j["stream"] for j in tails] == list(r.STREAM_ORDER)
    inst_tail = tails[0]
    assert (inst_tail["date_since"], inst_tail["date_until"]) == (D("2026-10-11"), D("2026-10-24"))
    back = [j for j in jobs if j["stream"] == "installs" and j["kind"] == "backfill"]
    assert back[0]["date_until"] == D("2026-10-10")          # граница хвоста установок 14 дн.
    assert back[0]["date_since"] > back[-1]["date_since"]    # новые окна первыми
    assert back[-1]["date_since"] == D("2024-08-01")
    assert all(j["priority"] == 1 for j in back)
    p = next(j for j in jobs if j["stream"] == "purchases")["params"]
    assert p["event_name"] == "purchase" and p["date_dimension"] == "receive"
    assert p["date_since"].endswith("00:00:00") and p["date_until"].endswith("23:59:59")


def test_plan_nothing_to_backfill_when_covered():
    covered = {"sessions": _days(r.STREAMS["sessions"].backfill_from, "2026-10-17")}
    jobs = [j for j in r.plan(D("2026-10-25"), covered) if j["stream"] == "sessions"]
    assert [j["kind"] for j in jobs] == ["tail"]


def test_stale_queries_by_age_in_msk():
    now = datetime(2026, 10, 24, 12, 0, tzinfo=r._MSK)
    qs = [{"request_id": "a", "create_time": "2026-10-24 05:00:00"},   # 7 ч
          {"request_id": "b", "create_time": "2026-10-24 11:00:00"}]   # 1 ч
    assert [q["request_id"] for q in r.stale_queries(qs, now, hours=6)] == ["a"]


def test_to_table_rows_sessions_aggregated_per_device_day():
    rows = [{"appmetrica_device_id": "d1", "session_start_datetime": "2026-10-20 10:00:00"},
            {"appmetrica_device_id": "d1", "session_start_datetime": "2026-10-20 18:00:00"},
            {"appmetrica_device_id": "d2", "session_start_datetime": "2026-10-21 01:00:00"},
            {"appmetrica_device_id": "", "session_start_datetime": "2026-10-21 01:00:00"}]
    out = sorted(r.to_table_rows(r.STREAMS["sessions"], rows, 7, D("2026-10-20"), D("2026-10-21")))
    assert out == [(D("2026-10-20"), "d1", 2, 7), (D("2026-10-21"), "d2", 1, 7)]


def test_to_table_rows_refuses_rows_outside_job_window():
    rows = [{"appmetrica_device_id": "d1", "install_datetime": "2026-09-30 23:59:00"}]
    with pytest.raises(RuntimeError, match="вне окна"):
        r.to_table_rows(r.STREAMS["installs"], rows, 1, D("2026-10-01"), D("2026-10-31"))


def test_to_table_rows_purchases_day_is_receive_date():
    rows = [{"appmetrica_device_id": "d1", "event_datetime": "2026-10-19 23:50:00",
             "event_receive_datetime": "2026-10-20 00:10:00", "event_json": "{}",
             "country_iso_code": "RU", "city": "Moscow"}]
    out = r.to_table_rows(r.STREAMS["purchases"], rows, 3, D("2026-10-20"), D("2026-10-20"))
    assert out[0][0] == D("2026-10-20") and out[0][2] == "2026-10-19 23:50:00"


class _Resp:
    def __init__(self, shards):
        self.headers = {"Shards-Unavailable": shards}


def test_check_shards():
    r.check_shards(_Resp("0"))
    with pytest.raises(r.ShardsUnavailable):
        r.check_shards(_Resp("2"))


@pytest.fixture
def journal(monkeypatch):
    log = []
    monkeypatch.setattr(r, "set_status", lambda c, i, s, e=None, polled=False: log.append(("status", s)))
    monkeypatch.setattr(r, "retry_or_fail", lambda c, j, e: log.append(("retry", e)))
    monkeypatch.setattr(r, "store", lambda c, s, i, a, b, rows: log.append(("store", len(rows))))
    return log


JOB = {"id": 1, "stream": "installs", "kind": "tail", "date_since": D("2026-10-11"),
       "date_until": D("2026-10-24"), "params": {}, "status": "planned", "attempts": 0, "requested_at": None}


def test_step_does_not_order_while_queue_busy(journal, monkeypatch):
    monkeypatch.setattr(r, "fetch_once", lambda *a: pytest.fail("заказ при занятой очереди"))
    assert r.step(None, "t", None, active=1) == ("queue-busy", None)
    assert journal == []


def test_step_202_keeps_polling_same_job(journal, monkeypatch):
    monkeypatch.setattr(r, "next_planned", lambda c: dict(JOB))
    monkeypatch.setattr(r, "fetch_once", lambda *a: (202, ""))
    what, job = r.step(None, "t", None, active=0)
    assert what == "wait" and job["id"] == 1 and journal == [("status", "requested")]


def test_step_200_stores(journal, monkeypatch):
    rows = [{"appmetrica_device_id": "d1", "install_datetime": "2026-10-12 10:00:00"}]
    monkeypatch.setattr(r, "fetch_once", lambda *a: (200, rows))
    assert r.step(None, "t", dict(JOB, status="requested"), active=0) == ("loaded", None)
    assert journal == [("store", 1)]


def test_step_shards_unavailable_retries_without_store(journal, monkeypatch):
    def boom(*a):
        raise r.ShardsUnavailable("Shards-Unavailable=1")
    monkeypatch.setattr(r, "fetch_once", boom)
    assert r.step(None, "t", dict(JOB), active=0)[0] == "shards"
    assert journal[0][0] == "retry" and all(k != "store" for k, _ in journal)


def test_target_table_suffix_validated(monkeypatch):
    class Cur:
        def __init__(self):
            self.sql = []

        def execute(self, q):
            self.sql.append(q)
    monkeypatch.delenv("APPM_TABLE_SUFFIX", raising=False)
    assert r.target_table(Cur(), "lime_app_installs") == "lime_app_installs"
    monkeypatch.setenv("APPM_TABLE_SUFFIX", "_shadow")
    c = Cur()
    assert r.target_table(c, "lime_app_installs") == "lime_app_installs_shadow"
    assert "LIKE lime_app_installs INCLUDING ALL" in c.sql[0]
    monkeypatch.setenv("APPM_TABLE_SUFFIX", "; drop table x")
    with pytest.raises(ValueError):
        r.target_table(Cur(), "lime_app_installs")
