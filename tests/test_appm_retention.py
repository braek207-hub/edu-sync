# -*- coding: utf-8 -*-
"""Сырьё AppMetrica со сроком хранения и заморозка витрин до RAW_START (переезд на DSA)."""
import json
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from sync import appmetrica_raw as r
from sync import lime_appmetrica as la

D = date.fromisoformat


class Cur:
    def __init__(self):
        self.sql = []
        self.rowcount = 7

    def execute(self, sql, args=None):
        self.sql.append((sql, args))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Conn:
    def __init__(self):
        self.cur = Cur()
        self.commits = 0

    def cursor(self, *a, **k):
        return self.cur

    def commit(self):
        self.commits += 1


def test_purchase_json_keeps_only_what_facts_read():
    full = json.dumps({"transaction_id": "T1", "value": 7800, "currency": "RUB",
                       "items": [{"id": "A"}] * 20, "shipping": 300, "coupon": "X"})
    slim = json.loads(r.purchase_json(full))
    assert slim == {"transaction_id": "T1", "value": 7800, "currency": "RUB"}
    # Факт покупки из урезанного payload тот же, что из полного.
    ev = lambda j: [{"appmetrica_device_id": "d", "event_datetime": "2026-10-24 10:00:00", "event_json": j}]
    assert la.purchase_facts(ev(full)) == la.purchase_facts(ev(r.purchase_json(full)))


def test_purchase_json_leaves_broken_payload_as_is():
    assert r.purchase_json("не json") == "не json"
    assert r.purchase_json(None) is None
    assert r.purchase_json("[1]") == "[1]"


def test_to_table_rows_slims_purchases():
    rows = [{"appmetrica_device_id": "d", "event_datetime": "2026-10-24 10:00:00",
             "event_receive_datetime": "2026-10-24 10:01:00",
             "event_json": json.dumps({"transaction_id": "T", "value": 1, "items": [1, 2]})}]
    out = r.to_table_rows(r.STREAMS["purchases"], rows, 1, D("2026-10-24"), D("2026-10-24"))
    assert json.loads(out[0][4]) == {"transaction_id": "T", "value": 1}


def test_read_from_clamps_to_raw_start_and_retention(monkeypatch):
    monkeypatch.setattr(r, "RAW_START", D("2026-10-23"))
    monkeypatch.setenv("APPM_TODAY", "2026-11-10")
    assert r.read_from("installs", D("2026-04-01")) == D("2026-10-23")
    assert r.read_from("purchases", D("2026-11-01")) == D("2026-11-01")
    # Сессии лежат 14 дней: старше — уже удалены.
    assert r.read_from("sessions", D("2026-10-23")) == D("2026-10-27")


def test_read_before_raw_start_returns_empty_without_coverage_check(monkeypatch):
    monkeypatch.setattr(r, "RAW_START", D("2026-10-23"))
    monkeypatch.setattr(r, "require_coverage", lambda *a: pytest.fail("окно до сырья не проверяется"))
    assert r.read_installations(Conn(), "2026-09-01", "2026-09-30") == []
    assert r.read_purchase_events(Conn(), "2026-09-01", "2026-09-30") == []
    assert r.read_sessions_day(Conn(), "2026-09-01", "2026-09-30") == []
    assert r.read_deeplinks(Conn(), "2026-09-01", "2026-09-30") == []


def test_retention_covers_every_reader_window():
    # Окна витрин в днях (максимум по календарю) — сырья должно хватать с запасом.
    # sync_window(N): с 1-го числа месяца N−1 назад — не больше N×31 дня.
    assert r.STREAMS["installs"].keep_days >= 12 * 31           # атрибуция install_source, 12 мес.
    assert r.STREAMS["purchases"].keep_days >= 7 * 31           # когорты lime_appmetrica, 7 мес.
    assert r.STREAMS["deeplinks"].keep_days >= 90 + 7           # last-touch разделов + хвост
    assert r.STREAMS["sessions"].keep_days >= 10 + 1            # пересборка install_source
    for s in r.STREAMS.values():
        assert s.keep_days > s.tail_days


def test_prune_db_deletes_rows_and_coverage_older_than_retention():
    c = Conn()
    out = r.prune_db(c, D("2026-11-10"))
    assert set(out) == set(r.STREAM_ORDER) and c.commits == 1
    deletes = [(s, a) for s, a in c.cur.sql if s.startswith("DELETE")]
    assert ("DELETE FROM appm_sessions_day WHERE date < %s", (D("2026-10-27"),)) in deletes
    assert ("DELETE FROM appm_coverage WHERE stream = %s AND day < %s",
            ("sessions", D("2026-10-27"))) in deletes
    assert ("DELETE FROM appm_installs WHERE install_date < %s",
            (D("2026-11-10") - timedelta(days=400),)) in deletes


def test_rewrite_keeps_history_before_edge():
    cur = Cur()
    rows = [(D("2026-10-23"), "p", "", "", 1, 0, 0.0)]
    with patch.object(la.psycopg2.extras, "execute_values"):
        la._rewrite(cur, "lime_app_cohorts", "lime_app_cohorts", ("date", "publisher"), rows, 500, D("2026-10-23"))
    assert cur.sql[0] == ("DELETE FROM lime_app_cohorts WHERE date >= %s", (D("2026-10-23"),))


def test_rewrite_refuses_rows_before_edge():
    with pytest.raises(RuntimeError, match="замороженную"):
        la._rewrite(Cur(), "lime_app_cohorts", "lime_app_cohorts", ("date",),
                    [(D("2026-09-30"),)], 500, D("2026-10-23"))


def test_install_source_skips_window_entirely_before_raw_start(monkeypatch):
    from sync import lime_app_install_source as src
    monkeypatch.setattr(r, "RAW_START", D("2026-10-23"))
    monkeypatch.setenv("APP_SOURCE_FROM", "2026-10-01")
    monkeypatch.setenv("APP_SOURCE_TO", "2026-10-20")
    monkeypatch.setenv("APPM_TODAY", "2026-10-29")
    with patch.object(src, "load_vk_entity_map", side_effect=AssertionError("не должен читать")):
        assert src.sync_lime_app_install_source() == 0
