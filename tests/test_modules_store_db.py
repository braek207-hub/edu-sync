# -*- coding: utf-8 -*-
"""Хранилище модулей на живой базе.

DATABASE_URL здесь не годится: в развёрнутых окружениях он указывает на прод.
Тест идёт только на явно заданную тестовую базу и только с путём к DDL миграции.
"""

import os
import threading
from datetime import datetime, timedelta, timezone

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


# module_previews ведёт миграция этапа 2 из той же папки; обе DDL повторяемы (IF NOT EXISTS,
# перенос goal_ids после первого наката — no-op), поэтому накатываются на каждый тест.
DDL_STAGE2 = "20261018213700_direct_modules_rules.sql"


def _cleanup(cur):
    for t in ("module_previews", "module_findings", "module_runs", "project_modules"):
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
    for path in (DDL_PATH, os.path.join(os.path.dirname(DDL_PATH), DDL_STAGE2)):
        with open(path, encoding="utf-8") as fh:
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


def test_approve_auto_only_open_with_action_and_no_decision_trace(live):
    run = store.start_run(SLUG, "placements", "auto")
    ids = store.apply_reconciled(SLUG, "placements", run, Reconciled(
        insert=[_f("a.ru"), _f("b.ru", action=False)]))
    assert store.approve_auto(SLUG, "placements", []) == []
    assert store.approve_auto(SLUG, "placements", ids) == [ids[0]]
    # повторно и по чужому модулю не проходит
    assert store.approve_auto(SLUG, "placements", ids) == []
    assert store.approve_auto(SLUG, "queries", ids) == []
    live.execute("SELECT status, decided_at, decided_by FROM module_findings WHERE id = %s",
                 (ids[0],))
    assert live.fetchone() == ("approved", None, None)
    assert store.set_outcome(ids[0], "applied", ApplyResult(ok=True, response={}))


def test_last_run_started_filters_by_status(live):
    bad = store.start_run(SLUG, "queries", "report")
    store.finish_run(bad, "error", {}, error="x")
    assert store.last_run_started(SLUG, "queries") is not None
    assert store.last_run_started(SLUG, "queries", ("ok", "skipped")) is None
    good = store.start_run(SLUG, "queries", "report")
    store.finish_run(good, "skipped", {})
    assert store.last_run_started(SLUG, "queries", ("ok", "skipped")) is not None


def test_apply_phase_rows_are_not_scans(live):
    scan = store.start_run(SLUG, "queries", "approve")
    store.finish_run(scan, "ok", {"findings": 0})
    live.execute("UPDATE module_runs SET started_at = now() - interval '1 day' WHERE id = %s",
                 (scan,))
    applied = store.start_run(SLUG, "queries", "approve", phase="apply")
    live.execute("SELECT summary->>'phase' FROM module_runs WHERE id = %s", (applied,))
    assert live.fetchone()[0] == "apply"
    store.finish_run(applied, "ok", {"phase": "apply", "apply": {"applied": 1}})
    live.execute("SELECT started_at FROM module_runs WHERE id = %s", (scan,))
    scan_started = live.fetchone()[0]
    assert store.last_run_started(SLUG, "queries") == scan_started
    assert store.last_run_started(SLUG, "queries", ("ok", "skipped")) == scan_started


def test_stale_running_runs_marked_interrupted(live):
    old = store.start_run(SLUG, "audit", "report")
    fresh = store.start_run(SLUG, "audit", "report", phase="apply")
    done = store.start_run(SLUG, "audit", "report")
    store.finish_run(done, "ok", {})
    live.execute("UPDATE module_runs SET started_at = now() - interval '81 minutes' "
                 "WHERE id IN (%s, %s)", (old, done))
    assert store.fail_stale_runs(SLUG, timedelta(minutes=80)) == 1
    live.execute("SELECT id, status, summary->>'reason', finished_at IS NOT NULL, error "
                 "FROM module_runs WHERE id IN (%s, %s, %s) ORDER BY id", (old, fresh, done))
    rows = live.fetchall()
    assert rows[0][:4] == (old, "error", "прерван", True) and "80 мин" in rows[0][4]
    assert rows[1][1] == "running" and rows[2][1] == "ok"
    # день прерванный прогон не закрывает
    assert store.last_run_started(SLUG, "audit", ("ok", "skipped")) is not None
    assert store.fail_stale_runs("__other_slug__", timedelta(minutes=80)) == 0


def test_auto_never_reapproves_human_rolled_back_action(live):
    run = store.start_run(SLUG, "placements", "auto")
    fid = _insert_applied(run, _f("a.ru"))
    live.execute("UPDATE module_findings SET status = 'applied' WHERE id = %s", (fid,))
    store.request_rollback(SLUG, "placements", ["a.ru"])
    assert store.set_outcome(fid, "rolled_back", ApplyResult(ok=True, response={}), rollback=True)
    # окно подавления давно прошло — находка вернулась предложением под другим правилом
    live.execute("UPDATE module_findings SET outcome_at = now() - interval '40 days' "
                 "WHERE id = %s", (fid,))
    back = _f("a.ru")
    back.rule = "spam"
    other = _f("b.ru")
    again = store.apply_reconciled(SLUG, "placements", run, Reconciled(insert=[back, other]))
    assert len(again) == 2
    assert store.approve_auto(SLUG, "placements", again) == [again[1]]
    live.execute("SELECT status FROM module_findings WHERE id = %s", (again[0],))
    assert live.fetchone()[0] == "open"
    # человек одобряет сам — это его решение, раннер его применит
    live.execute("UPDATE module_findings SET status = 'approved' WHERE id = %s", (again[0],))
    assert again[0] in [p.id for p in store.pending(SLUG)]


def test_empty_strings_are_stored_as_null(live):
    run = store.start_run(SLUG, "audit", "report")
    f = Finding(rule="proj", severity="info", title="t", login="", campaign_id=None,
                campaign_name="", object_type="", object_id="x", details={})
    (fid,) = store.apply_reconciled(SLUG, "audit", run, Reconciled(insert=[f]))
    live.execute("SELECT login, campaign_name, object_type FROM module_findings WHERE id = %s",
                 (fid,))
    assert live.fetchone() == (None, None, None)
    (back,) = [s for s in store.existing_findings(SLUG, "audit") if s.id == fid]
    assert (back.login, back.campaign_name, back.object_type) == ("", "", "")
    # находка уровня проекта с login NULL по-прежнему закрывается прочитанным прогоном
    now = datetime.now(timezone.utc)
    assert reconcile("audit", [back], [], now, ["acc"]).resolve == [fid]


def test_load_project_modules_include_off(live):
    live.execute("INSERT INTO project_modules (dashboard_slug, module_key, mode, settings) "
                 "VALUES (%s, 'queries', 'off', '{\"max_per_run\": 7}')", (SLUG,))
    assert store.load_project_modules(SLUG) == []
    (row,) = store.load_project_modules(SLUG, include_off=True)
    assert row["mode"] == "off" and row["settings"] == {"max_per_run": 7}


class _Shared:
    """Одно соединение на все вызовы хранилища: close() не закрывает его по-настоящему."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


# project_sources ведёт другая миграция Panda (20261017153700_project_onboarding_core.sql),
# на scratch-базе её может не быть. Таблица — копия той DDL временной таблицей сессии:
# временные таблицы стоят в search_path первыми и умирают вместе с соединением, так что
# база после теста остаётся какой была, а живая public.project_sources, если она есть,
# тестом не задевается. Внешних ключей нет: временной таблице на постоянную ссылаться нельзя.
_SOURCES_DDL = """
CREATE TEMP TABLE project_sources (
  id                bigserial PRIMARY KEY,
  dashboard_slug    text NOT NULL,
  kind              text NOT NULL CHECK (kind IN ('direct', 'metrika')),
  external_id       text NOT NULL,
  yandex_account_id bigint NOT NULL,
  settings          jsonb NOT NULL DEFAULT '{}',
  status            text NOT NULL DEFAULT 'new',
  UNIQUE (dashboard_slug, kind, external_id)
)"""


@pytest.fixture
def sources(live, monkeypatch):
    conn = psycopg2.connect(TEST_URL)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(_SOURCES_DDL)
    monkeypatch.setattr(db, "_new_connection", lambda: _Shared(conn))
    yield lambda kind, ext, settings: cur.execute(
        "INSERT INTO project_sources (dashboard_slug, kind, external_id, yandex_account_id, "
        "settings) VALUES (%s, %s, %s, 1, %s::jsonb)", (SLUG, kind, ext, settings))
    conn.close()


def test_project_lead_goals_from_metrika_sources(sources):
    assert store.project_lead_goals(SLUG) == []
    sources("metrika", "100", '{"goals": [{"id": 30, "name": "Заявка", "role": "lead"},'
                              ' {"id": 10, "name": "Корзина", "role": "step"},'
                              ' {"id": 20, "name": "Качественный лид", "role": "quality_lead"}],'
                              ' "ecommerce": false, "result": "goal"}')
    # Второй счётчик: цели складываются, повтор не дублируется, кривые пропускаются.
    sources("metrika", "200", '{"goals": [{"id": 30, "name": "Заявка", "role": "lead"},'
                              ' {"id": "5", "name": "x", "role": "lead"}, "мусор",'
                              ' {"id": 40, "name": "Звонок", "role": "lead"}]}')
    sources("metrika", "300", '{}')
    # Кабинет Директа целей не несёт, даже если в settings что-то похожее.
    sources("direct", "login1", '{"currency": "RUB", "goals": [{"id": 99, "role": "lead"}]}')
    # quality_lead срабатывает на той же заявке, что lead: при целях lead он не добавляется.
    assert store.project_lead_goals(SLUG) == [30, 40]
    assert store.project_lead_goals("__no_such_project__") == []


def test_project_lead_goals_quality_only_when_no_lead(sources):
    sources("metrika", "100", '{"goals": [{"id": 20, "name": "Качественный лид", "role": "quality_lead"},'
                              ' {"id": 10, "name": "Корзина", "role": "step"}]}')
    assert store.project_lead_goals(SLUG) == [20]


# --- превью (module_previews) ---

def _queue(cur, module="placements", ago=0, status="queued", started_ago=None,
           progress="{}", rule_id=None):
    cur.execute(
        "INSERT INTO module_previews (dashboard_slug, module_key, settings, rule_id, status, "
        "progress, created_at, started_at) VALUES (%s, %s, %s::jsonb, %s, %s, %s::jsonb, "
        "now() - make_interval(secs => %s), "
        "CASE WHEN %s::float8 IS NULL THEN NULL ELSE now() - make_interval(secs => %s) END) "
        "RETURNING id",
        (SLUG, module, '{"top_n": 5, "имя": "черновик"}', rule_id, status, progress, ago,
         started_ago, started_ago or 0))
    return cur.fetchone()[0]


def _preview_row(cur, pid):
    cur.execute("SELECT status, error, result, progress, started_at, finished_at "
                "FROM module_previews WHERE id = %s", (pid,))
    return cur.fetchone()


def test_claim_preview_full_cycle(live):
    assert store.claim_preview(SLUG) is None
    pid = _queue(live, rule_id="aaa111")
    row = store.claim_preview(SLUG)
    assert row["id"] == pid and row["module_key"] == "placements" and row["rule_id"] == "aaa111"
    assert row["settings"] == {"top_n": 5, "имя": "черновик"} and row["started_at"] is not None
    assert _preview_row(live, pid)[0] == "running"
    # идёт — второй раз не выдаётся
    assert store.claim_preview(SLUG) is None

    store.preview_progress(pid, {"step": "логин", "logins_done": 1, "logins_total": 3})
    progress = _preview_row(live, pid)[3]
    assert progress["step"] == "логин" and progress["logins_total"] == 3 and progress["at"]

    result = {"would": [{"object": "сайт.рф"}], "totals": {"would": 1}}
    assert store.finish_preview(pid, "done", result) is True
    status, error, saved, _p, _s, finished = _preview_row(live, pid)
    assert (status, error, saved) == ("done", None, result) and finished is not None
    # закрытое не переписывается
    assert store.finish_preview(pid, "error", None, error="поздно") is False
    assert _preview_row(live, pid)[0] == "done"


def test_finish_preview_error_without_result(live):
    pid = _queue(live)
    store.claim_preview(SLUG)
    assert store.finish_preview(pid, "error", None, error="RuntimeError: boom")
    assert _preview_row(live, pid)[:3] == ("error", "RuntimeError: boom", None)


def test_claim_replaces_older_queued_of_same_module(live):
    old = _queue(live, "placements", ago=60)
    q = _queue(live, "queries", ago=30)
    new = _queue(live, "placements", ago=10)
    # из оставшихся старейшее — queries; старое placements заменено новым
    assert store.claim_preview(SLUG)["id"] == q
    status, error = _preview_row(live, old)[:2]
    assert status == "error" and store.PREVIEW_REPLACED in error and "заменено новым" in error
    assert store.claim_preview(SLUG)["id"] == new
    assert store.claim_preview(SLUG) is None


def test_running_preview_blocks_its_module_until_stale(live):
    running = _queue(live, "placements", ago=600, status="running", started_ago=300)
    queued = _queue(live, "placements", ago=10)
    other = _queue(live, "queries", ago=5)
    # running дожидается: свежее превью того же модуля ждёт, соседний модуль — нет
    assert store.claim_preview(SLUG)["id"] == other
    assert store.claim_preview(SLUG) is None
    assert _preview_row(live, running)[0] == "running"

    # 25 минут без хода — прерван, очередь модуля освобождается
    live.execute("UPDATE module_previews SET started_at = now() - interval '25 minutes' "
                 "WHERE id = %s", (running,))
    assert store.claim_preview(SLUG)["id"] == queued
    status, error = _preview_row(live, running)[:2]
    assert status == "error" and "прерван" in error


def test_running_preview_with_fresh_progress_is_not_stale(live):
    # старт давно, но ход был минуту назад: большой проект честно идёт дольше 20 минут
    pid = _queue(live, "placements", ago=3600, status="running", started_ago=1800)
    live.execute("UPDATE module_previews SET progress = jsonb_build_object('logins_done', 3, "
                 "'at', now() - interval '1 minute') WHERE id = %s", (pid,))
    _queue(live, "placements", ago=10)
    assert store.claim_preview(SLUG) is None
    assert _preview_row(live, pid)[0] == "running"


def test_claim_preview_atomic_across_connections(live):
    pid = _queue(live)
    gate = threading.Barrier(4)
    got = []

    def claim():
        gate.wait()
        got.append(store.claim_preview(SLUG))

    threads = [threading.Thread(target=claim) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [r["id"] for r in got if r] == [pid] and len(got) == 4


def test_claim_preview_skips_row_locked_by_other_connection(live):
    pid = _queue(live)
    other = psycopg2.connect(TEST_URL)
    try:
        with other.cursor() as cur:
            cur.execute("SELECT id FROM module_previews WHERE id = %s FOR UPDATE", (pid,))
            # строку держит другой раннер: не ждём его, а уходим ни с чем
            assert store.claim_preview(SLUG) is None
        other.rollback()
        assert store.claim_preview(SLUG)["id"] == pid
    finally:
        other.close()


def test_claim_preview_by_id(live):
    done = _queue(live, status="done")
    live.execute("UPDATE module_previews SET result = '{\"would\": []}', error = 'x' "
                 "WHERE id = %s", (done,))
    row = store.claim_preview(SLUG, preview_id=done)
    assert row["id"] == done
    status, error, result = _preview_row(live, done)[:3]
    assert (status, error, result) == ("running", None, None)
    # идущее и чужое по id не берутся
    assert store.claim_preview(SLUG, preview_id=done) is None
    assert store.claim_preview("__other_slug__", preview_id=done) is None


def test_cleanup_previews_keeps_fresh_and_running(live):
    old = _queue(live, ago=8 * 86400, status="done")
    old_running = _queue(live, ago=8 * 86400, status="running", started_ago=60)
    fresh = _queue(live, ago=3600, status="error")
    assert store.cleanup_previews(SLUG) == 1
    live.execute("SELECT id FROM module_previews WHERE dashboard_slug = %s ORDER BY id", (SLUG,))
    assert [r[0] for r in live.fetchall()] == [old_running, fresh]
    assert old not in (old_running, fresh)
    assert store.cleanup_previews(SLUG, older_than=timedelta(minutes=30)) == 1


def test_approve_auto_skips_propose(live):
    run = store.start_run(SLUG, "placements", "auto")
    plain = _f("plain.ru")
    propose = _f("propose.ru")
    propose.details = dict(propose.details, propose=True)
    ids = store.apply_reconciled(SLUG, "placements", run, Reconciled(insert=[plain, propose]))
    assert store.approve_auto(SLUG, "placements", ids) == [ids[0]]
    live.execute("SELECT status FROM module_findings WHERE id = %s", (ids[1],))
    assert live.fetchone()[0] == "open"
