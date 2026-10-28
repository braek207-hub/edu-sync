"""sync/progress.py: журнал хода прогона не роняет синк и не шумит в базу."""

import json

import pytest

from sync import progress
from sync.progress import Reporter


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        if self.conn.fail:
            raise RuntimeError("db down")
        self.conn.calls.append((sql, args))

    def fetchone(self):
        return self.conn.found


class FakeConn:
    def __init__(self, fail=False, found=None):
        self.fail = fail
        self.found = found
        self.calls = []
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def writes(conn):
    return [json.loads(args[0]) for sql, args in conn.calls if "progress = coalesce" in sql and isinstance(args, tuple)]


@pytest.fixture
def run_file(tmp_path):
    (tmp_path / "bjorn_direct.run").write_text("inv-1 42 1700000000 -\n", encoding="utf-8")
    return tmp_path


def make(run_file, conn, clock=None, invocation="inv-1"):
    return Reporter(
        "bjorn_direct",
        connect=lambda: conn,
        clock=clock or Clock(),
        run_dir=str(run_file),
        invocation=invocation,
    )


def test_no_unit_is_noop(monkeypatch):
    called = []
    r = Reporter(None, connect=lambda: called.append(1))
    r.report("x", 1, 2)
    r.result(rows=5)
    assert list(r.track([1, 2], "x")) == [1, 2]
    r.flush()
    assert called == []
    assert not r.enabled


def test_module_level_noop_without_env(monkeypatch):
    monkeypatch.delenv(progress.UNIT_ENV, raising=False)
    monkeypatch.setattr(progress, "_reporter", None)
    progress.report("x", 1, 2)
    progress.result(rows=1)
    assert progress._reporter.unit is None


def test_run_id_from_run_file_and_payload(run_file):
    conn = FakeConn()
    r = make(run_file, conn)
    r.report("direct.cabinets", 1, 4, "Директ · кабинеты")
    (sql, args), = conn.calls
    body = json.loads(args[0])
    assert args[1] == 42
    assert body["stage"] == "direct.cabinets" and body["done"] == 1 and body["total"] == 4
    assert body["label"] == "Директ · кабинеты" and body["updated_at"].endswith("Z")


def test_foreign_invocation_falls_back_to_db(run_file):
    conn = FakeConn(found=(77,))
    r = make(run_file, conn, invocation="other")
    r.report("s", 1, 2)
    assert "order by id desc" in conn.calls[0][0]
    assert conn.calls[0][1] == ("bjorn_direct",)
    assert conn.calls[1][1][1] == 77


def test_not_found_writes_nothing(tmp_path):
    conn = FakeConn(found=None)
    r = make(tmp_path, conn)
    r.report("s", 1, 2)
    r.report("t", 1, 2)
    assert len(conn.calls) == 1  # только поиск, один раз


def test_throttle_same_stage(run_file):
    conn, clock = FakeConn(), Clock()
    r = make(run_file, conn, clock)
    r.report("days", 1, 30)
    clock.t += 0.5
    r.report("days", 2, 30)
    clock.t += 0.5
    r.report("days", 3, 30)
    assert [w["done"] for w in writes(conn)] == [1]
    clock.t += 1.5  # 2.5 с от первой записи
    r.report("days", 4, 30)
    assert [w["done"] for w in writes(conn)] == [1, 4]


def test_new_stage_written_immediately(run_file):
    conn, clock = FakeConn(), Clock()
    r = make(run_file, conn, clock)
    r.report("a", 1, 2)
    clock.t += 0.1
    r.report("b")
    assert [w["stage"] for w in writes(conn)] == ["a", "b"]
    assert writes(conn)[1]["done"] is None


def test_flush_writes_pending(run_file):
    conn, clock = FakeConn(), Clock()
    r = make(run_file, conn, clock)
    r.report("days", 1, 30)
    clock.t += 0.1
    r.report("days", 29, 30)
    r.flush()
    assert [w["done"] for w in writes(conn)] == [1, 29]
    r.flush()
    assert len(writes(conn)) == 2


def test_track_counts_and_finishes(run_file):
    conn, clock = FakeConn(), Clock()
    r = make(run_file, conn, clock)
    seen = []
    for x in r.track(["a", "b", "c"], "cab", "Кабинеты"):
        seen.append(x)
        clock.t += 3
    assert seen == ["a", "b", "c"]
    assert [(w["done"], w["total"]) for w in writes(conn)] == [(0, 3), (1, 3), (2, 3), (3, 3)]


def test_result_uses_named_params(run_file):
    conn = FakeConn()
    r = make(run_file, conn)
    r.result(rows=1240, days=3)
    sql, args = conn.calls[-1]
    assert "jsonb_strip_nulls" in sql
    assert args["rows"] == 1240 and args["days"] == 3 and args["id"] == 42
    assert args["at"].endswith("Z")


def test_errors_are_swallowed_and_disable(run_file, capsys):
    conn, clock = FakeConn(fail=True), Clock()
    r = make(run_file, conn, clock)
    for i in range(5):
        r.report(f"s{i}", i, 5)
        r.result(rows=1)
    err = capsys.readouterr().err
    assert "progress: запись не удалась" in err
    assert "выключен" in err
    assert not r.enabled
    assert conn.closed


def test_connect_failure_swallowed(run_file, capsys):
    def boom():
        raise OSError("refused")

    r = Reporter("bjorn_direct", connect=boom, run_dir=str(run_file), invocation="inv-1")
    r.report("s", 1, 2)
    assert "refused" in capsys.readouterr().err


def test_recovers_after_single_failure(run_file):
    conn, clock = FakeConn(fail=True), Clock()
    r = make(run_file, conn, clock)
    r.report("a")
    conn.fail = False
    r.report("b")
    assert [w["stage"] for w in writes(conn)] == ["b"]
    assert r.enabled
