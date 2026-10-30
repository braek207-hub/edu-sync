# -*- coding: utf-8 -*-
"""Ход прогона модулей в sync_runs.progress и «Остановить» (SIGTERM → 143) без потерь и дублей."""

import json
import signal

import psycopg2.errors
import pytest

from sync.modules import audit as audit_mod
from sync.modules import cancel as cancel_mod
from sync.modules import runner
from sync.modules import store as store_mod
from sync.modules.accounts import DirectAccount
from sync.modules.run_progress import RunProgress
from sync.modules.types import Context, RunOutput
from tests.test_modules_runner import NOW, FakeModule, FakeStore, _finding, _row, _stored
# run_file — фикстура: pytest находит её по имени в модуле теста
from tests.test_progress import FakeConn, make, run_file, writes  # noqa: F401

ACC3 = [DirectAccount("a1", "t", "RUB", "X"), DirectAccount("a2", "t", "RUB", "X"),
        DirectAccount("a3", "t", "KZT", "X")]


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, stage, done, total, label, extra):
        self.calls.append(dict(extra, stage=stage, done=done, total=total, label=label))

    def stages(self):
        return [c["stage"] for c in self.calls]


class LoginModule(FakeModule):
    """Как placements/queries: ctx.progress после каждого кабинета."""

    def run(self, ctx: Context) -> RunOutput:
        self.trace.append(("run", self.KEY))
        for i, acc in enumerate(ctx.accounts, 1):
            ctx.progress({"step": acc.login, "logins_done": i, "logins_total": len(ctx.accounts)})
        return RunOutput(findings=self.findings, summary={})


class StopModule(FakeModule):
    """Остановка посреди обхода кабинетов: обработчик SIGTERM зовётся внутри module.run."""

    def __init__(self, mkey, switch, **kw):
        super().__init__(mkey, **kw)
        self.switch = switch
        self.after = False

    def run(self, ctx: Context) -> RunOutput:
        self.trace.append(("run", self.KEY))
        ctx.progress({"step": "a1", "logins_done": 1, "logins_total": len(ctx.accounts)})
        self.switch.request(signal.SIGTERM, None)
        self.after = True  # сюда не дойти: скан только читает, обрывается сразу
        return RunOutput(findings=self.findings)


class CancelStore(FakeStore):
    def cancel_run(self, rid, summary):
        self.runs[rid].update(status="cancelled", summary=dict(summary, cancelled=True))
        self.calls.append(("cancel_run", self.runs[rid]["key"]))


def _go(st, modules, rec=None, switch=None, accts=ACC3):
    return runner.run_project("lime", store=st, accounts=lambda s: (accts, []), modules=modules,
                              now=NOW, log=lambda m: None, agent_lock_fn=lambda s: None,
                              cabinets=lambda *a: None, cancel=switch or cancel_mod.Switch(),
                              progress=RunProgress(rec or Recorder()))


# ---------------------------------------------------------------- ход


def test_progress_module_k_of_m_and_current_login():
    rec = Recorder()
    st = CancelStore([_row("placements", "report"), _row("queries", "report")])
    rc = _go(st, {"placements": LoginModule("placements"), "queries": LoginModule("queries")},
             rec)
    assert rc == 0
    scans = [c for c in rec.calls if c["stage"] == "modules.queries"]
    assert scans[0]["label"] == "Запросы · кабинеты"
    assert scans[0]["module_label"] == "Запросы"
    assert (scans[0]["module_index"], scans[0]["modules_total"]) == (2, 2)
    assert scans[0]["modules_done"] == 1
    assert scans[0]["modules"] == ["placements", "queries"]
    # до обхода — первый кабинет, после i пройденных — следующий, в конце — никто
    assert [(c["done"], c["total"], c["login"]) for c in scans] == [
        (0, 3, "a1"), (1, 3, "a2"), (2, 3, "a3"), (3, 3, None)]
    assert rec.calls[0]["stage"] == "modules.prepare"
    # поле прошлого модуля не висит: каждый ход несёт все ключи
    assert all("part" in c and "login" in c for c in rec.calls)


def test_plan_has_only_due_modules():
    rec = Recorder()
    st = CancelStore([_row("placements", "report"), _row("audit", "report")])
    # аудит сегодня уже прошёл — в этом прогоне его нет, «модуль 1 из 1»
    st.last_ok["audit"] = st.last_started["audit"] = NOW
    _go(st, {"placements": LoginModule("placements"), "audit": LoginModule("audit")}, rec)
    assert rec.calls[-1]["modules"] == ["placements"]
    # до плана (подготовка) модулей ещё 0 — «неизвестно», дальше 1
    assert {c["modules_total"] for c in rec.calls if c["stage"] != "modules.prepare"} == {1}


def test_apply_phase_reported():
    rec = Recorder()
    pend = [_stored(1, "approved")]
    st = CancelStore([_row("placements", "approve")], pending=pend)
    _go(st, {"placements": LoginModule("placements")}, rec)
    apply = [c for c in rec.calls if c["stage"] == "modules.apply"]
    assert apply and apply[0]["label"] == "Модули · применение решений"
    assert apply[0]["module"] is None and (apply[0]["done"], apply[0]["total"]) == (0, 1)


def test_audit_reports_parts_per_cabinet(monkeypatch):
    seen = []
    monkeypatch.setattr(audit_mod, "fetch_snapshot", lambda acc, win, step: (
        [step(p) for p in audit_mod.PARTS],
        audit_mod.Snapshot(login=acc.login, campaigns=[], groups=[], ads=[], keywords=[],
                           audiences_by_group={}, impressions_3d={}, unavailable={},
                           impressions_from=win[0]))[1])
    ctx = Context(slug="lime", module_key="audit", mode="report", settings={},
                  accounts=ACC3[:2], now=NOW, log=lambda m: None, progress=seen.append)
    audit_mod.run(ctx)
    first = [p for p in seen if p["step"] == "a1"]
    assert first[0] == {"step": "a1", "logins_done": 0, "logins_total": 2, "part": "кампании",
                        "part_done": 0, "part_total": len(audit_mod.PARTS)}
    assert first[-1] == {"step": "a1", "logins_done": 1, "logins_total": 2}
    assert seen[-1]["logins_done"] == 2


def test_audit_part_goes_to_sync_runs_fields():
    rec = Recorder()
    prog = RunProgress(rec)
    prog.plan(["audit"])
    cb = prog.module("audit", ACC3)
    cb({"step": "a2", "logins_done": 1, "logins_total": 3, "part": "фразы", "part_done": 5,
        "part_total": 8})
    last = rec.calls[-1]
    assert (last["login"], last["part"], last["part_done"], last["part_total"]) == (
        "a2", "фразы", 5, 8)
    cb({"step": "a2", "logins_done": 2, "logins_total": 3})
    assert rec.calls[-1]["part"] is None and rec.calls[-1]["login"] == "a3"


def test_reporter_writes_extra_fields(run_file):  # noqa: F811
    conn = FakeConn()
    r = make(run_file, conn)
    r.report("modules.audit", 1, 3, "Аудит · кабинеты", {"module": "audit", "login": "a2"})
    body = writes(conn)[0]
    assert body["module"] == "audit" and body["login"] == "a2"
    assert (body["stage"], body["done"], body["total"]) == ("modules.audit", 1, 3)
    # кириллица уходит как есть, а не \\u-последовательностями
    assert "Аудит" in [a for s, a in conn.calls if "progress = coalesce" in s][0][0]


# ---------------------------------------------------------------- остановка


def test_cancel_mid_scan_closes_run_cancelled_and_skips_rest():
    sw = cancel_mod.Switch()
    stop = StopModule("placements", sw, findings=[_finding()])
    q = LoginModule("queries")
    st = CancelStore([_row("placements", "auto"), _row("queries", "report")])
    rc = _go(st, {"placements": stop, "queries": q}, switch=sw)
    assert rc == cancel_mod.EXIT_CODE == 143
    assert not stop.after
    # находки скана не записаны и не применены — нечему дублироваться на следующем такте
    assert ("apply_reconciled", "placements") not in st.calls
    assert ("approve_auto", "placements") not in st.calls
    assert st.runs[1]["status"] == "cancelled" and st.runs[1]["summary"]["cancelled"] is True
    assert ("run", "queries") not in q.trace
    assert ("start_run", "queries") not in st.calls


def test_cancel_during_apply_waits_for_outcome():
    """SIGTERM вне скана: применение доходит до записи итога, сканы уже не начинаются."""
    sw = cancel_mod.Switch()

    class ApplyThenStop(LoginModule):
        def apply(self, ctx, items):
            res = super().apply(ctx, items)
            sw.request(signal.SIGTERM, None)  # обработчик не бросает: мы не в interruptible
            return res

    mod = ApplyThenStop("placements")
    st = CancelStore([_row("placements", "approve")], pending=[_stored(7, "approved")])
    rc = _go(st, {"placements": mod}, switch=sw)
    assert rc == 143
    assert st.outcomes == [(7, "applied", False)]
    assert st.runs[1]["phase"] == "apply" and st.runs[1]["status"] == "ok"
    assert ("run", "placements") not in mod.trace


def test_cancel_in_preview_closes_preview():
    sw = cancel_mod.Switch()
    stop = StopModule("placements", sw)
    st = CancelStore([_row("placements", "report")],
                     previews=[{"id": 5, "module_key": "placements", "settings": {},
                                "rule_id": None, "status": "queued"}])
    rc = _go(st, {"placements": stop}, switch=sw)
    assert rc == 143
    assert st._preview(5)["status"] == "error"
    assert "остановлен" in st._preview(5)["error"]
    assert ("start_run", "placements") not in st.calls


def test_real_sigterm_handler_interrupts_scan():
    sw = cancel_mod.Switch()
    old = signal.getsignal(signal.SIGTERM)
    try:
        cancel_mod.install(sw)
        with pytest.raises(cancel_mod.Cancelled):
            with sw.interruptible():
                signal.raise_signal(signal.SIGTERM)
        # вне участка чтения обработчик только поднимает флаг
        sw2 = cancel_mod.Switch()
        cancel_mod.install(sw2)
        signal.raise_signal(signal.SIGTERM)
        assert sw2.requested
        with pytest.raises(cancel_mod.Cancelled):
            sw2.check()
    finally:
        signal.signal(signal.SIGTERM, old)


def test_cancelled_is_not_swallowed_by_module_except_exception():
    sw = cancel_mod.Switch()
    with pytest.raises(cancel_mod.Cancelled):
        with sw.interruptible():
            try:
                sw.request()
            except Exception:  # так модуль ловит сбой одного кабинета
                pytest.fail("Cancelled проглочен как сбой кабинета")


def test_store_cancel_run_falls_back_without_migration(monkeypatch):
    calls = []

    def finish(run_id, status, summary, error=None):
        calls.append((status, json.dumps(summary, sort_keys=True), error))
        if status == "cancelled":
            raise psycopg2.errors.CheckViolation("module_runs_status_check")

    monkeypatch.setattr(store_mod, "finish_run", finish)
    store_mod.cancel_run(3, {"reason": "остановлен вручную"})
    assert [c[0] for c in calls] == ["cancelled", "error"]
    assert '"cancelled": true' in calls[1][1] and calls[1][2] == "остановлен вручную"


def test_modules_run_main_returns_143(monkeypatch):
    from sync import modules_run
    installed = []
    monkeypatch.setattr(modules_run.cancel, "install", lambda: installed.append(1))
    monkeypatch.setattr(modules_run, "run_project", lambda *a, **kw: cancel_mod.EXIT_CODE)
    assert modules_run.main(["--project", "lime"]) == 143
    assert installed == [1]
