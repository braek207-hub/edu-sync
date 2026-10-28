"""Версии входов и готовность витрин (P1/C2 аудита Panda-BI).

Проверяются свойства, ради которых хелпер написан:
  • версия пишется тем же курсором (той же транзакцией), что данные, и под SAVEPOINT —
    не накаченная миграция не роняет запись данных;
  • готовность без функции в базе — пропуск, а настоящая ошибка — громкая;
  • писатели входов витрины кабинетов LIME зовут версию и готовность.
SQL самих функций проверяется в EduDash на настоящем Postgres (__tests__/data-versions-sql.test.ts).
"""
from datetime import date

import psycopg2
import pytest

import sync.data_versions as dv


class FakeCursor:
    def __init__(self, fail_on=None, result=(42,)):
        self.sql = []
        self.fail_on = fail_on
        self.result = result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.fail_on and self.fail_on in sql:
            raise psycopg2.Error("function bump_data_version does not exist")

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.result


def test_bump_inside_savepoint_returns_version():
    cur = FakeCursor()
    assert dv.bump(cur, "lime", "lime_google_ads_stats", date(2026, 10, 27), "2026-10-28T00:00:00", 5) == 42
    stmts = [s for s, _ in cur.sql]
    assert stmts[0] == "SAVEPOINT data_version"
    assert "bump_data_version" in stmts[1]
    assert cur.sql[1][1] == ("lime", "lime_google_ads_stats", "2026-10-27", "2026-10-28", 5)
    assert stmts[2] == "RELEASE SAVEPOINT data_version"


def test_bump_without_migration_rolls_back_to_savepoint_only():
    cur = FakeCursor(fail_on="bump_data_version")
    assert dv.bump(cur, "lime", "lime_vk_ads_stats", "2026-10-01", "2026-10-14") is None
    assert cur.sql[-1][0] == "ROLLBACK TO SAVEPOINT data_version"


def test_bump_empty_range_is_noop():
    cur = FakeCursor()
    assert dv.bump(cur, "lime", "x", None, "2026-10-01") is None
    assert dv.bump_rows(cur, "lime", "x", []) is None
    assert cur.sql == []


def test_bump_rows_spans_dates_of_dicts_and_tuples():
    cur = FakeCursor()
    dv.bump_rows(cur, "lime", "lime_direct_stats",
                 [{"date": "2026-10-27"}, {"date": "2026-09-30"}, {"date": "2026-10-01"}])
    assert cur.sql[1][1] == ("lime", "lime_direct_stats", "2026-09-30", "2026-10-27", 3)
    cur = FakeCursor()
    dv.bump_rows(cur, "lime", "lime_stats", [("2026-10-02", 1), ("2026-10-05", 2)])
    assert cur.sql[1][1][2:4] == ("2026-10-02", "2026-10-05")


class FakeConn:
    def __init__(self, cur):
        self.cur = cur
        self.committed = self.rolled_back = self.closed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def test_ready_reports_rebuilt_months(monkeypatch):
    cur = FakeCursor(result=[("mart_lime_cabinet_daily", 1)])
    conn = FakeConn(cur)
    monkeypatch.setattr(dv.psycopg2, "connect", lambda *a, **k: conn)
    assert dv.ready("lime", "lime_google_ads_stats", "postgresql://x/y?sslmode=require") == 1
    assert cur.sql[0][1] == ("lime", "lime_google_ads_stats")
    assert conn.committed and conn.closed


def test_ready_without_function_is_skip(monkeypatch):
    class UndefinedFunction(psycopg2.Error):
        pgcode = "42883"

    class Cur(FakeCursor):
        def execute(self, sql, params=None):
            raise UndefinedFunction("нет функции")

    conn = FakeConn(Cur())
    monkeypatch.setattr(dv.psycopg2, "connect", lambda *a, **k: conn)
    assert dv.ready("lime", "lime_vk_ads_stats", "postgresql://x/y") == 0
    assert conn.rolled_back and conn.closed


def test_ready_real_failure_is_loud(monkeypatch):
    class Cur(FakeCursor):
        def execute(self, sql, params=None):
            raise psycopg2.Error("lock timeout")

    monkeypatch.setattr(dv.psycopg2, "connect", lambda *a, **k: FakeConn(Cur()))
    with pytest.raises(psycopg2.Error):
        dv.ready("lime", "lime_vk_ads_stats", "postgresql://x/y")


def test_ready_without_url_is_skip(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert dv.ready("lime", "x") == 0


def test_vk_upsert_bumps_window_and_runs_ready(monkeypatch):
    import sync.lime_vk_ads as vk

    cur = FakeCursor()

    class Conn(FakeConn):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setattr(vk.psycopg2, "connect", lambda *a, **k: Conn(cur))
    monkeypatch.setattr(vk.psycopg2.extras, "execute_batch", lambda *a, **k: None)
    readies = []
    monkeypatch.setattr(vk.data_versions, "ready", lambda *a: readies.append(a[:2]))
    vk._upsert([{"date": "2026-10-20"}], "2026-10-14", "2026-10-27")
    bump = [p for s, p in cur.sql if "bump_data_version" in s]
    assert bump == [("lime", "lime_vk_ads_stats", "2026-10-14", "2026-10-27", 1)]
    assert readies == [("lime", "lime_vk_ads_stats")]


def test_direct_upsert_bumps_rows_and_runs_ready(monkeypatch):
    import sync.lime_direct as ld

    cur = FakeCursor()

    class Conn(FakeConn):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setattr(ld.psycopg2, "connect", lambda *a, **k: Conn(cur))
    monkeypatch.setattr(ld.psycopg2.extras, "execute_batch", lambda *a, **k: None)
    readies = []
    monkeypatch.setattr(ld.data_versions, "ready", lambda *a: readies.append(a[:2]))
    ld._upsert([{"date": "2026-10-26"}, {"date": "2026-10-27"}])
    bump = [p for s, p in cur.sql if "bump_data_version" in s]
    assert bump == [("lime", "lime_direct_stats", "2026-10-26", "2026-10-27", 2)]
    assert readies == [("lime", "lime_direct_stats")]
