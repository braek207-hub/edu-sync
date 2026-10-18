# -*- coding: utf-8 -*-
import dataclasses

import pytest
from psycopg2 import sql
from psycopg2.extras import Json

from sync.projects import store as S
from sync.projects.direct_fetch import DirectRow
from sync.projects.metrika_fetch import PurchaseRow

SRC = S.Source(7, "acme", "direct", "login", 3, {}, "ok", None)


def render(q) -> str:
    if isinstance(q, str):
        return q
    if isinstance(q, sql.Composed):
        return "".join(render(x) for x in q.seq)
    if isinstance(q, sql.Identifier):
        return '"' + ".".join(q.strings) + '"'
    if isinstance(q, sql.SQL):
        return q.string
    if isinstance(q, sql.Placeholder):
        return "%s"
    raise TypeError(q)


class Cur:
    def __init__(self, log, rows=()):
        self.log, self.rows = log, list(rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, q, args=None):
        self.log.append(("execute", render(q), args))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0]


class Conn:
    def __init__(self, rows=()):
        self.log, self.rows = [], rows

    def cursor(self):
        return Cur(self.log, self.rows)

    def commit(self):
        raise AssertionError("store не коммитит")


@pytest.fixture
def batches(monkeypatch):
    out = []

    def fake(cur, q, rows, page_size):
        out.append((render(q), rows, page_size))
        cur.log.append(("batch", render(q), None))

    monkeypatch.setattr(S, "execute_batch", fake)
    return out


def test_replace_window_deletes_then_inserts_with_source_and_slug(batches):
    conn = Conn()
    rows = [DirectRow("2026-10-01", 5, "c", 1.5, 2, 30), DirectRow("2026-10-02", 6, "d", 0, 0, 0)]
    assert S.replace_window(conn, "project_direct_daily", SRC, "2026-10-01", "2026-10-02", rows) == 2
    assert [e[0] for e in conn.log] == ["execute", "batch"]
    assert 'DELETE FROM "project_direct_daily" WHERE source_id = %s AND "date" BETWEEN' in conn.log[0][1]
    assert conn.log[0][2] == (7, "2026-10-01", "2026-10-02")
    q, data, page = batches[0]
    assert q.startswith('INSERT INTO "project_direct_daily" ("source_id", "dashboard_slug", "date", "campaign_id"')
    assert page == 500
    assert all(r[0] == 7 and r[1] == "acme" for r in data)
    assert data[0] == (7, "acme", "2026-10-01", 5, "c", 1.5, 2, 30)


def test_empty_rows_still_clear_window(batches):
    conn = Conn()
    assert S.replace_window(conn, "project_metrika_goals", SRC, "2026-10-01", "2026-10-02", []) == 0
    assert len(conn.log) == 1 and conn.log[0][1].startswith("DELETE FROM")
    assert batches == []


def test_unknown_table_rejected(batches):
    conn = Conn()
    for bad in ("users", "project_metrika_purchases", 'x" OR 1=1 --'):
        with pytest.raises(ValueError):
            S.replace_window(conn, bad, SRC, "2026-10-01", "2026-10-02", [])
    assert conn.log == []


def test_purchases_upsert_on_order(batches):
    conn = Conn()
    r = PurchaseRow("o1", "2026-10-03", "ad", "yandex", "", "", "", "", 1, 99.0)
    assert S.replace_purchases(conn, SRC, "2026-10-01", "2026-10-05", [r]) == 1
    assert '"purchase_date" BETWEEN' in conn.log[0][1]
    q = batches[0][0]
    assert "ON CONFLICT (source_id, order_id) DO UPDATE SET" in q
    assert 'WHERE EXCLUDED.purchase_date >= "project_metrika_purchases".purchase_date' in q
    assert "updated_at = now()" in q
    for f in dataclasses.fields(PurchaseRow):
        if f.name != "order_id":
            assert f'"{f.name}" = EXCLUDED."{f.name}"' in q
    assert '"source_id" = EXCLUDED' not in q and '"order_id" = EXCLUDED' not in q
    assert batches[0][1][0][:3] == (7, "acme", "o1")


def test_mark_error_sets_status():
    conn = Conn()
    S.mark_error(conn, 7, "boom")
    _, q, args = conn.log[0]
    assert "status = 'error'" in q and "last_error = %s" in q
    assert args == ("boom", 7)


def test_mark_ok():
    conn = Conn()
    S.mark_ok(conn, 7)
    q = conn.log[0][1]
    assert "status = 'ok'" in q and "last_synced_at = now()" in q and "last_error = NULL" in q


def test_backfill_json_serialized():
    conn = Conn()
    S.save_backfill(conn, 7, {"state": "done"})
    S.save_backfill(conn, 7, {"state": "failed"}, status="error")
    a1, a2 = conn.log[0][2], conn.log[1][2]
    assert isinstance(a1[0], Json) and a1[0].adapted == {"state": "done"} and a1[1] == 7
    assert "status" not in conn.log[0][1]
    assert isinstance(a2[0], Json) and a2[1:] == ("error", 7) and "status = %s" in conn.log[1][1]


def test_load_sources_maps_columns_and_filters():
    conn = Conn(rows=[(1, "acme", "direct", "lg", 3, {"a": 1}, "ok", None)])
    got = S.load_daily_sources(conn, "acme")
    assert got == [S.Source(1, "acme", "direct", "lg", 3, {"a": 1}, "ok", None)]
    q, args = conn.log[0][1], conn.log[0][2]
    assert "dashboard_slug AS slug" in q and "yandex_account_id AS account_id" in q
    assert "status IN ('ok','error')" in q and q.endswith("ORDER BY id") and args == ["acme"]
    conn2 = Conn()
    S.load_queued_sources(conn2)
    assert "backfill->>'state' IN ('queued','running')" in conn2.log[0][1] and conn2.log[0][2] == []


def test_locks():
    conn = Conn(rows=[(True,)])
    assert S.try_lock_source(conn, 7) is True
    S.unlock_source(conn, 7)
    assert "pg_try_advisory_lock" in conn.log[0][1] and conn.log[0][2] == (7311, 7)
    assert "pg_advisory_unlock" in conn.log[1][1] and conn.log[1][2] == (7311, 7)
