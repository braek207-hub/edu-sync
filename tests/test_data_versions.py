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


def test_bump_dimension_marks_epoch_month():
    cur = FakeCursor()
    assert dv.bump_dimension(cur, "lime", "lime_vk_entities", 7) == 42
    assert cur.sql[1][1] == ("lime", "lime_vk_entities", dv.DIMENSION_DAY, dv.DIMENSION_DAY, 7)
    assert dv.DIMENSION_DAY == "1970-01-01"


def test_rewrite_bumps_only_months_whose_content_changed(monkeypatch):
    """Полная перезапись: версия — только месяцам, где содержимое изменилось (новый, ушедший,
    поменявшийся); не тронутые месяцы версию не получают. Теневая копия версию не трогает."""
    import sync.lime_appmetrica as am

    class Cur(FakeCursor):
        def __init__(self, snapshots):
            super().__init__()
            self.snapshots = list(snapshots)

        def fetchall(self):
            return self.snapshots.pop(0)

    # до: install_date — март, апрель; purchase_date — апрель. После: апрель тот же, март ушёл,
    # май появился, purchase_date апреля поменялся.
    cur = Cur([
        [("2026-03-01", "a"), ("2026-04-01", "b")],
        [("2026-04-01", "p")],
        [("2026-04-01", "b"), ("2026-05-01", "c")],
        [("2026-04-01", "p2")],
    ])
    monkeypatch.setattr(am.psycopg2.extras, "execute_values", lambda *a, **k: None)
    am._rewrite(cur, "lime_app_install_purchase_daily", "lime_app_install_purchase_daily",
                ("install_date", "purchase_date", "orders"), [(date(2026, 5, 1), date(2026, 5, 2), 1)], 10, date.min)
    am._rewrite(cur, "lime_app_installs", "lime_app_installs_shadow", ("date",), [], 10, date.min)
    bumps = [p for s, p in cur.sql if "bump_data_version" in s]
    assert [b[2] for b in bumps] == ["2026-03-01", "2026-04-01", "2026-05-01"]
    fingerprint = cur.sql[0][0]
    assert "row(install_date, purchase_date, orders)::text" in fingerprint
    assert "updated_at" not in fingerprint


def test_unchanged_rewrite_bumps_nothing():
    before = {("date", "2026-09-01"): "x", ("date", "2026-10-01"): "y"}
    cur = FakeCursor()
    assert dv.bump_changed_months(cur, "lime", "lime_app_installs", before, dict(before)) == []
    assert cur.sql == []


# ── P8: входы EDU, BJORN, mnf, Polina и справочник целей LIME ──


class QueueCursor(FakeCursor):
    """fetchone отдаёт заранее заданные ответы по очереди; версия — 42."""

    def __init__(self, answers):
        super().__init__()
        self.answers = list(answers)

    def fetchone(self):
        if self.sql and "bump_data_version" in self.sql[-1][0]:
            return (42,)
        return self.answers.pop(0)

    @property
    def rowcount(self):
        return 3


def _db_with(monkeypatch, cur):
    from contextlib import contextmanager

    import sync.db as db

    @contextmanager
    def conn():
        yield FakeConn(cur)

    monkeypatch.setattr(db, "get_connection", conn)
    monkeypatch.setattr(db, "ensure_schema", lambda: None)
    monkeypatch.setattr(db.psycopg2.extras, "execute_batch", lambda *a, **k: None)
    return db


def _bumps(cur):
    return [p for s, p in cur.sql if "bump_data_version" in s]


def test_crm_window_replace_bumps_window_up_to_old_tail(monkeypatch):
    """Окно открыто справа: удалённый хвост позже новых строк тоже отмечается."""
    cur = QueueCursor([(10, date(2026, 10, 28))])
    db = _db_with(monkeypatch, cur)
    rows = [{"date": "2026-10-01", "leads": 6}, {"date": "2026-10-25", "leads": 5}]
    db.replace_crm_leads(rows)
    assert _bumps(cur) == [("edunetwork", "crm_leads", "2026-10-01", "2026-10-28", 2)]


def test_lead_details_bump_covers_payment_dates_and_rewritten_old_rows(monkeypatch):
    # guard → (строк, MAX created_date); затем крайние created/payment_date старых строк.
    cur = QueueCursor([(1, date(2026, 10, 27)), (date(2026, 3, 2), date(2026, 10, 28))])
    db = _db_with(monkeypatch, cur)
    rows = [{"lead_id": "L1", "created_date": "2026-09-01", "payment_date": "2026-10-20"},
            {"lead_id": "L2", "created_date": "2026-10-27", "payment_date": ""}]
    db.upsert_lead_details(rows)
    span_sql, span_params = cur.sql[1]
    assert "lead_id = ANY" in span_sql and span_params == ("2026-09-01", ["L1", "L2"])
    assert _bumps(cur) == [("edunetwork", "crm_lead_details", "2026-03-02", "2026-10-28", 2)]


def test_delete_window_bumps_deleted_days_and_skips_empty_tail(monkeypatch):
    cur = QueueCursor([(date(2026, 10, 27),), (None,)])
    db = _db_with(monkeypatch, cur)
    assert db.delete_window_from("meshnflesh", "meshnflesh_metrica_sources", "date", "2026-10-01") == 3
    assert _bumps(cur) == [("meshnflesh", "meshnflesh_metrica_sources", "2026-10-01", "2026-10-27", 3)]
    db.delete_window_from("polinarepik", "polinarepik_metrica_purchases", "purchase_date", "2026-10-28")
    assert len(_bumps(cur)) == 1
    assert "purchase_date >= %s" in cur.sql[-1][0]


def test_goal_catalog_bumps_dimension_only_when_content_changed(monkeypatch):
    import sync.lime_metrika_goals as g

    monkeypatch.setattr(g.psycopg2.extras, "execute_values", lambda *a, **k: None)
    catalog = [{"goal_id": "1", "name": "Корзина", "type": "action", "is_retargeting": False}]

    same = QueueCursor([("abc",), ("abc",)])
    g._write_catalog(FakeConn(same), catalog)
    assert _bumps(same) == []
    assert "synced_at" not in same.sql[0][0]

    changed = QueueCursor([("abc",), ("def",)])
    g._write_catalog(FakeConn(changed), catalog)
    assert _bumps(changed) == [("lime", "lime_metrika_goals", dv.DIMENSION_DAY, dv.DIMENSION_DAY, 1)]


def test_bjorn_replace_window_bumps_after_write_via_rpc(monkeypatch):
    import sync.bjorn.common as common

    monkeypatch.setenv("SUPABASE_URL", "https://x")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "k")
    client = common.SupabaseRest()
    calls = []
    monkeypatch.setattr(client, "request", lambda method, path, **kw: calls.append((method, path, kw)))
    client.replace_window("bjorn", "marketing_daily_fact", [{"date": "2026-10-27"}],
                          "date,campaign_id", "2026-10-27", "2026-10-28")
    assert [c[:2] for c in calls] == [("DELETE", "marketing_daily_fact")] * 2 + [
        ("POST", "marketing_daily_fact"), ("POST", "rpc/bump_data_version")]
    assert calls[-1][2]["json_body"] == {"p_project": "bjorn", "p_source": "marketing_daily_fact",
                                         "p_from": "2026-10-27", "p_to": "2026-10-28", "p_rows": 1}


def test_bjorn_version_failure_does_not_fail_sync(monkeypatch):
    import sync.bjorn.common as common

    monkeypatch.setenv("SUPABASE_URL", "https://x")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "k")
    client = common.SupabaseRest()

    def boom(*a, **k):
        raise RuntimeError("PGRST202")

    monkeypatch.setattr(client, "request", boom)
    client.bump_data_version("bjorn", "bjorn_ad_daily", "2026-10-01", "2026-10-02")
