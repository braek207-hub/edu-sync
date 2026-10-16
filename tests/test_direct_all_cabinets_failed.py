# -*- coding: utf-8 -*-
"""Директ EDU (sync.yml → main.py → sync.direct): сбой кабинетов не должен быть зелёным.

Было: если у всех кабинетов «Reports API: превышено число попыток», sync_direct_api_range
возвращал 0, main.py выходил с кодом 0, витрина стояла. Стало: ни один не загрузился —
исключение до записи; частичный сбой — загрузившиеся записаны, прогон всё равно красный;
full-перезагрузка при частичном сбое не удаляет окно (упавший кабинет потерял бы историю).
"""
import types

import pytest

from sync import direct as D

CLIENTS = [{"login": f"acc{i}", "goal_ids": [], "project": None, "sheet_name": ""} for i in range(4)]


@pytest.fixture
def db(monkeypatch):
    calls = {"upsert": [], "delete": []}
    fake = types.SimpleNamespace(
        upsert_direct_stats=lambda rows: calls["upsert"].append(rows) or len(rows),
        delete_direct_stats_from=lambda d: calls["delete"].append(d) or 0,
    )
    monkeypatch.setitem(__import__("sys").modules, "sync.db", fake)
    monkeypatch.setattr(D, "_direct_clients", lambda: CLIENTS)
    return calls


def _reports(monkeypatch, failing):
    def fetch(login, date_from, date_to, goals):
        if login in failing:
            raise RuntimeError(f"Директ API [{login}]: превышено число попыток")
        return [{"date": date_from, "campaign_id": login, "cost": 1}]

    monkeypatch.setattr(D, "_fetch_report", fetch)


def test_all_cabinets_failed_raises_before_any_write(db, monkeypatch):
    _reports(monkeypatch, failing={c["login"] for c in CLIENTS})

    with pytest.raises(RuntimeError, match="ни один кабинет не загрузился"):
        D.sync_direct_api_range("2026-10-09", "2026-10-16")
    assert db == {"upsert": [], "delete": []}


def test_partial_failure_writes_the_rest_then_fails(db, monkeypatch):
    _reports(monkeypatch, failing={"acc2"})

    with pytest.raises(RuntimeError, match=r"записано 3 строк.*acc2"):
        D.sync_direct_api_range("2026-10-09", "2026-10-16")
    assert [r["campaign_id"] for r in db["upsert"][0]] == ["acc0", "acc1", "acc3"]


def test_partial_failure_in_full_mode_deletes_nothing(db, monkeypatch):
    _reports(monkeypatch, failing={"acc0"})

    with pytest.raises(RuntimeError, match="удаление не выполнялось"):
        D.sync_direct_api_range("2026-01-01", "2026-10-16", replace_from=True)
    assert db == {"upsert": [], "delete": []}


def test_all_loaded_but_empty_is_still_success(db, monkeypatch):
    monkeypatch.setattr(D, "_fetch_report", lambda *a: [])

    assert D.sync_direct_api_range("2026-10-09", "2026-10-16") == 0
    assert db["upsert"] == []


def test_all_loaded_writes_and_returns_count(db, monkeypatch):
    _reports(monkeypatch, failing=set())

    assert D.sync_direct_api_range("2026-10-09", "2026-10-16") == 4


def test_main_exits_nonzero_when_no_cabinet_loaded(db, monkeypatch):
    import main

    _reports(monkeypatch, failing={c["login"] for c in CLIENTS})
    monkeypatch.setattr(main, "_resolve_env", lambda: None)
    for k, v in {"DATABASE_URL": "postgresql:///test", "DIRECT_TOKEN": "t",
                 "DIRECT_CLIENTS_JSON": "[]", "EDU_SOURCES": "direct",
                 "DIRECT_SOURCE": "api", "DIRECT_SYNC_MODE": "incremental"}.items():
        monkeypatch.setenv(k, v)

    assert main.main() == 1
