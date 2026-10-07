# -*- coding: utf-8 -*-
"""Снимок настроек для журнала изменений Panda-BI — у всех дашбордов, не только EDU.

Главный риск — чужие кампании под чужим именем: пока в strategy_snapshots нет колонки
dashboard, вся таблица — EDU, и снимок LIME/BJORN туда лечь не должен. Второй — снимок
казахстанского кабинета LIME в тенге, который журнал показал бы рублями.
"""

import os

import pytest

from sync import direct_settings_cabinets as C
from sync import edu_direct_settings as eds
from sync import lime_direct
from sync import strategy_snapshots as SS


class _Cur:
    def __init__(self, has_column):
        self.has_column = has_column
        self.executed = []
        self.rowcount = 5
        self._last = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._last = sql

    def fetchone(self):
        if "information_schema" in self._last:
            return (1,) if self.has_column else None
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self.cur = cur
        self.committed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_pg(monkeypatch, has_column):
    cur = _Cur(has_column)
    monkeypatch.setattr(SS.psycopg2, "connect", lambda url: _Conn(cur))
    return cur


def test_sql_with_column_tags_rows_with_dashboard_and_reads_its_table():
    sql = SS.snapshot_sql("bjorn", with_dashboard_column=True, only_ids=False)
    assert "FROM bjorn_campaign_settings" in sql
    assert "dashboard, date, campaign_id" in sql
    assert "dashboard     = EXCLUDED.dashboard" in sql
    assert "ANY(" not in sql


def test_sql_without_column_is_the_legacy_edu_form():
    sql = SS.snapshot_sql("edunetwork", with_dashboard_column=False, only_ids=False)
    assert "FROM edu_campaign_settings" in sql
    assert "dashboard" not in sql
    assert "ON CONFLICT (date, campaign_id)" in sql


def test_sql_without_column_refuses_foreign_dashboard():
    with pytest.raises(ValueError):
        SS.snapshot_sql("lime", with_dashboard_column=False, only_ids=False)


def test_only_ids_limits_snapshot_to_run_campaigns():
    sql = SS.snapshot_sql("lime", with_dashboard_column=True, only_ids=True)
    assert "WHERE campaign_id = ANY(%(ids)s)" in sql


def test_foreign_dashboard_skips_until_migration(monkeypatch, capsys):
    cur = _fake_pg(monkeypatch, has_column=False)
    assert SS.write_snapshot("pg://x", "polinarepik") == 0
    # Проверили колонку — и больше ничего не писали.
    assert len(cur.executed) == 1
    assert "::warning::" in capsys.readouterr().out


def test_edu_keeps_writing_before_migration(monkeypatch):
    cur = _fake_pg(monkeypatch, has_column=False)
    assert SS.write_snapshot("pg://x", "edunetwork") == 5
    sql, params = cur.executed[-1]
    assert "FROM edu_campaign_settings" in sql and "dashboard" not in sql


def test_after_migration_every_dashboard_writes_its_slug(monkeypatch):
    cur = _fake_pg(monkeypatch, has_column=True)
    SS.write_snapshot("pg://x", "meshnflesh")
    sql, params = cur.executed[-1]
    assert "FROM mnf_campaign_settings" in sql
    assert params["dashboard"] == "meshnflesh"


def test_unknown_dashboard_is_an_error():
    with pytest.raises(ValueError):
        SS.write_snapshot("pg://x", "decortier")


def test_lime_kz_cabinet_is_not_snapshotted(monkeypatch):
    calls = []
    monkeypatch.setattr(lime_direct, "write_snapshot", lambda *a: calls.append(a))
    monkeypatch.setattr(lime_direct, "_pg_url", lambda: "pg://x")
    monkeypatch.setenv("LIME_DIRECT_SRC_CURRENCY", "KZT")
    lime_direct._snapshot_settings(["1"])
    assert calls == []
    monkeypatch.delenv("LIME_DIRECT_SRC_CURRENCY")
    lime_direct._snapshot_settings(["1", "2"])
    assert calls == [("pg://x", "lime", ["1", "2"])]


def test_parse_clients_json_same_forms_as_bjorn_sync():
    raw = '["a", {"login": "b", "token": "tb"}, {"client_login": "c"}, {"token": "x"}]'
    assert C.parse_clients_json(raw, "dflt") == [
        {"login": "a", "token": "dflt"},
        {"login": "b", "token": "tb"},
        {"login": "c", "token": "dflt"},
    ]
    with pytest.raises(RuntimeError):
        C.parse_clients_json("[]")


def _stub_cabinet_io(monkeypatch, fail_login=None):
    seen = []
    monkeypatch.setattr(eds, "_list_campaigns_for_login", lambda: {"7": "к"})

    def _sync(ids, names, table, extra_counter_ids):
        seen.append((eds._CURRENT_LOGIN, os.environ["DIRECT_TOKEN"], table, extra_counter_ids))
        if eds._CURRENT_LOGIN == fail_login:
            raise ValueError("boom")
        return len(ids)

    snaps = []
    monkeypatch.setattr(eds, "_sync_campaign_settings", _sync)
    monkeypatch.setattr(eds, "_pg_url", lambda: "pg://x")
    monkeypatch.setattr(C, "write_snapshot", lambda url, dash: snaps.append(dash))
    return seen, snaps


def test_bjorn_cabinet_walks_every_login_with_its_token(monkeypatch):
    monkeypatch.setenv(
        "BJORN_DIRECT_CLIENTS_JSON", '[{"login": "b1", "token": "t1"}, {"login": "b2", "token": "t2"}]'
    )
    monkeypatch.setenv("BJORN_METRICA_COUNTER_ID", "123")
    seen, snaps = _stub_cabinet_io(monkeypatch)
    assert C.sync_cabinet("bjorn") == 2
    assert seen == [
        ("b1", "t1", "bjorn_campaign_settings", [123]),
        ("b2", "t2", "bjorn_campaign_settings", [123]),
    ]
    assert snaps == ["bjorn"]


def test_polina_defaults_login_and_reads_yandex_token(monkeypatch):
    monkeypatch.delenv("POLINAREPIK_DIRECT_TOKEN", raising=False)
    monkeypatch.delenv("POLINAREPIK_DIRECT_CLIENT_LOGIN", raising=False)
    monkeypatch.setenv("POLINAREPIK_YANDEX_TOKEN", "py")
    seen, snaps = _stub_cabinet_io(monkeypatch)
    C.sync_cabinet("polinarepik")
    assert seen == [("polinarepik-wear", "py", "polinarepik_campaign_settings", [100764399])]
    assert snaps == ["polinarepik"]


def test_failed_login_makes_run_red_but_others_and_snapshot_go_through(monkeypatch):
    monkeypatch.setenv(
        "BJORN_DIRECT_CLIENTS_JSON", '[{"login": "b1", "token": "t1"}, {"login": "b2", "token": "t2"}]'
    )
    seen, snaps = _stub_cabinet_io(monkeypatch, fail_login="b1")
    with pytest.raises(RuntimeError) as err:
        C.sync_cabinet("bjorn")
    assert "b1" in str(err.value)
    assert [s[0] for s in seen] == ["b1", "b2"]
    assert snaps == ["bjorn"]


def test_meshnflesh_entrypoint_is_the_generic_cabinet(monkeypatch):
    monkeypatch.setenv("MESHNFLESH_DIRECT_TOKEN", "mt")
    seen, snaps = _stub_cabinet_io(monkeypatch)
    from sync import meshnflesh_direct_settings as M

    M.sync_meshnflesh_campaign_settings()
    assert seen == [("meshnflesh", "mt", "mnf_campaign_settings", [82116769])]
    assert snaps == ["meshnflesh"]
