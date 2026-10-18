# -*- coding: utf-8 -*-
"""Хранилище модулей на живой базе.

DATABASE_URL здесь не годится: в развёрнутых окружениях он указывает на прод.
Тест идёт только на явно заданную тестовую базу и только с путём к DDL миграции.
"""

import os
from datetime import datetime, timezone

import psycopg2
import pytest

from sync import db
from sync.modules import store
from sync.modules.lifecycle import Reconciled, reconcile
from sync.modules.types import ApplyResult, Finding

SLUG = "__test_modules__"
TEST_URL = os.environ.get("MODULES_TEST_DATABASE_URL")
DDL_PATH = os.environ.get("MODULES_DDL_PATH")

pytestmark = pytest.mark.skipif(
    not (TEST_URL and DDL_PATH),
    reason="нужны MODULES_TEST_DATABASE_URL и MODULES_DDL_PATH")


def _cleanup(cur):
    for t in ("module_findings", "module_runs", "project_modules"):
        cur.execute(f"DELETE FROM {t} WHERE dashboard_slug = %s", (SLUG,))
    cur.execute("DELETE FROM dashboards WHERE slug = %s", (SLUG,))


@pytest.fixture
def live(monkeypatch):
    # sync.db требует sslmode=require; локальный кластер без TLS, поэтому подменяем
    # только открытие соединения (URL берём из тестовой переменной, не из DATABASE_URL).
    monkeypatch.setattr(db, "_new_connection", lambda: psycopg2.connect(TEST_URL))
    conn = psycopg2.connect(TEST_URL)
    conn.autocommit = True
    cur = conn.cursor()
    with open(DDL_PATH, encoding="utf-8") as fh:
        cur.execute(fh.read())
    _cleanup(cur)
    cur.execute("INSERT INTO dashboards (id, slug, name) VALUES (%s, %s, %s)",
                (SLUG, SLUG, "test"))
    yield cur
    _cleanup(cur)
    conn.close()


def _f(obj, action=True, title="Площадка исключена"):
    return Finding(rule="junk", severity="warning", title=title, login="acc", campaign_id=42,
                   campaign_name="Кампания", object_type="site", object_id=obj,
                   details={"clicks": 3, "примечание": "цифры"},
                   action={"op": "exclude_site", "site": obj} if action else None)


def test_full_cycle(live):
    now = datetime.now(timezone.utc)
    run = store.start_run(SLUG, "placements", "approve")
    assert store.last_run_started(SLUG, "placements") is not None
    assert store.load_project_modules(SLUG) == []

    live.execute("INSERT INTO project_modules (dashboard_slug, module_key, mode) "
                 "VALUES (%s, 'placements', 'approve')", (SLUG,))
    mods = store.load_project_modules(SLUG)
    assert [(m["module_key"], m["mode"]) for m in mods] == [("placements", "approve")]

    seen = [_f("a.ru"), _f("b.ru"), _f("audit", action=False)]
    ids = store.apply_reconciled(
        SLUG, "placements", run,
        reconcile("placements", store.existing_findings(SLUG, "placements"), seen, now))
    assert len(ids) == 3

    # повторный прогон ничего не вставляет, а касается открытых
    ex = store.existing_findings(SLUG, "placements")
    r2 = reconcile("placements", ex, seen, now)
    assert not r2.insert and len(r2.touch) == 3
    assert store.apply_reconciled(SLUG, "placements", run, r2) == []

    # параллельная вставка той же живой находки не падает
    assert store.apply_reconciled(SLUG, "placements", run, Reconciled(insert=[_f("a.ru")])) == []

    # кириллица пережила запись
    live.execute("SELECT title, details->>'примечание' FROM module_findings WHERE id = %s",
                 (ids[0],))
    assert live.fetchone() == ("Площадка исключена", "цифры")

    # апрув человеком -> pending -> исход
    live.execute("UPDATE module_findings SET status = 'approved' WHERE id = %s", (ids[0],))
    pend = store.pending(SLUG)
    assert [p.id for p in pend] == [ids[0]] and pend[0].action["site"] == "a.ru"
    assert store.set_outcome(ids[0], "applied", ApplyResult(ok=True, response={"ok": "да"}))
    assert store.pending(SLUG) == []
    assert not store.set_outcome(ids[0], "approved", ApplyResult(ok=True, response={}))

    # откат
    assert store.request_rollback(SLUG, "placements", ["a.ru", "zzz"]) == 1
    assert store.request_rollback(SLUG, "placements", []) == 0
    assert [p.status for p in store.pending(SLUG)] == ["rollback_requested"]
    assert store.set_outcome(ids[0], "rolled_back", ApplyResult(ok=True, response={}), rollback=True)

    # аудит, которого прогон не увидел, закрывается
    r3 = reconcile("placements", store.existing_findings(SLUG, "placements"), [], now)
    assert r3.resolve == [ids[2]]
    store.apply_reconciled(SLUG, "placements", run, r3)
    live.execute("SELECT status FROM module_findings WHERE id = %s", (ids[2],))
    assert live.fetchone()[0] == "resolved"

    store.finish_run(run, "ok", {"найдено": 3})
    live.execute("SELECT status, summary->>'найдено', finished_at IS NOT NULL "
                 "FROM module_runs WHERE id = %s", (run,))
    assert live.fetchone() == ("ok", "3", True)


def test_project_lock_is_exclusive(live):
    with store.hold_project_lock(SLUG) as first:
        assert first is True
        with store.hold_project_lock(SLUG) as second:
            assert second is False
        with store.hold_project_lock("__other_slug__") as other:
            assert other is True
    with store.hold_project_lock(SLUG) as again:
        assert again is True


def _insert_applied(run, f):
    ids = store.apply_reconciled(SLUG, "placements", run, Reconciled(insert=[f]))
    assert len(ids) == 1
    return ids[0]


def test_refused_rollback_returns_to_applied_keeping_response(live):
    run = store.start_run(SLUG, "placements", "approve")
    fid = _insert_applied(run, _f("a.ru"))
    live.execute("UPDATE module_findings SET status = 'approved' WHERE id = %s", (fid,))
    assert store.set_outcome(fid, "applied", ApplyResult(ok=True, response={"ответ": "ок"}))
    assert store.request_rollback(SLUG, "placements", ["a.ru"], login="other") == 0
    assert store.request_rollback(SLUG, "placements", ["a.ru"], login="acc") == 1
    assert store.set_outcome(fid, "applied", ApplyResult(ok=False, response={"код": 1},
                                                         error="Директ отказал"), rollback=True)
    live.execute("SELECT status, response->>'ответ', response->'rollback'->>'error', error "
                 "FROM module_findings WHERE id = %s", (fid,))
    assert live.fetchone() == ("applied", "ок", "Директ отказал", "Директ отказал")
    # источник один: повторный исход с того же шага не проходит
    assert not store.set_outcome(fid, "rolled_back", ApplyResult(ok=True, response={}),
                                 rollback=True)


def test_seen_live_updates_only_last_seen_and_run(live):
    run = store.start_run(SLUG, "placements", "approve")
    fid = _insert_applied(run, _f("a.ru"))
    live.execute("UPDATE module_findings SET status = 'approved' WHERE id = %s", (fid,))
    run2 = store.start_run(SLUG, "placements", "approve")
    store.apply_reconciled(SLUG, "placements", run2, Reconciled(seen_live=[fid]))
    live.execute("SELECT status, run_id, details->>'clicks' FROM module_findings WHERE id = %s",
                 (fid,))
    assert live.fetchone() == ("approved", run2, "3")


def test_rolled_back_and_failed_are_loaded_for_suppression(live):
    now = datetime.now(timezone.utc)
    run = store.start_run(SLUG, "placements", "approve")
    a, b = _insert_applied(run, _f("a.ru")), _insert_applied(run, _f("b.ru"))
    live.execute("UPDATE module_findings SET status = 'approved' WHERE id = %s", (b,))
    assert store.set_outcome(b, "failed", ApplyResult(ok=False, response={}, error="e"))
    live.execute("UPDATE module_findings SET status = 'applied' WHERE id = %s", (a,))
    store.request_rollback(SLUG, "placements", ["a.ru"])
    assert store.set_outcome(a, "rolled_back", ApplyResult(ok=True, response={}), rollback=True)
    live.execute("SELECT decided_at, outcome_at FROM module_findings WHERE id IN (%s, %s)", (a, b))
    # decided_at — след решения человека, раннер его не пишет
    assert all(r[0] is None and r[1] is not None for r in live.fetchall())
    ex = store.existing_findings(SLUG, "placements")
    assert {e.status for e in ex} == {"rolled_back", "failed"}
    r = reconcile("placements", ex, [_f("a.ru"), _f("b.ru")], now)
    assert r.insert == []
    # давнее решение больше не держит
    live.execute("UPDATE module_findings SET decided_at = now(), "
                 "outcome_at = now() - interval '2 days' WHERE id = %s", (b,))
    ex = store.existing_findings(SLUG, "placements")
    assert [e.status for e in ex] == ["rolled_back"]
