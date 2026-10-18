# -*- coding: utf-8 -*-
"""Раннер модулей Директа на поддельных store и модулях: порядок фаз, замки, изоляция сбоев."""

import dataclasses
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from sync.modules import runner
from sync.modules.accounts import DirectAccount
from sync.modules.types import (ApplyResult, Context, Finding, RunOutput,
                                StoredFinding)

NOW = datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)  # 12:00 МСК
ACC = [DirectAccount("login1", "tok", "RUB", "X")]


def _finding(obj="site.ru", action=True) -> Finding:
    return Finding(rule="r", severity="warn", title="t", login="login1", campaign_id=1,
                   campaign_name="c", object_type="placement", object_id=obj, details={},
                   action={"op": "block"} if action else None)


def _stored(fid, status, key="placements", obj="old.ru") -> StoredFinding:
    return StoredFinding(id=fid, status=status, fingerprint=f"{key}:r:login1:1:{obj}",
                         last_seen_at=NOW, rule="r", severity="warn", title="t", login="login1",
                         campaign_id=1, campaign_name="c", object_type="placement",
                         object_id=obj, details={}, action={"op": "block"})


class FakeStore:
    def __init__(self, rows, pending=(), lock=True, existing=None, previews=(), goals=()):
        self.rows, self._pending, self.lock = rows, list(pending), lock
        self.existing = existing or {}
        self.calls: List[tuple] = []
        self.outcomes: List[tuple] = []
        self.runs: Dict[int, dict] = {}
        self.last_started: Dict[str, Optional[datetime]] = {}
        self.last_ok: Dict[str, Optional[datetime]] = {}
        self.outcome_ok = True
        self.previews: List[dict] = [dict(p) for p in previews]
        self.goals = list(goals)
        self.inserted: Dict[str, List[Finding]] = {}

    def project_lead_goals(self, slug):
        return list(self.goals)

    def claim_preview(self, slug, preview_id=None):
        self.calls.append(("claim_preview",))
        for p in self.previews:
            if p["status"] == "queued" and preview_id in (None, p["id"]):
                p["status"] = "running"
                return {k: v for k, v in p.items() if k != "status"}
        return None

    def _preview(self, pid):
        return next(p for p in self.previews if p["id"] == pid)

    def preview_progress(self, pid, progress):
        self._preview(pid).setdefault("progress", []).append(progress)

    def finish_preview(self, pid, status, result, error=None):
        self._preview(pid).update(status=status, result=result, error=error)
        self.calls.append(("finish_preview", pid, status))
        return True

    def cleanup_previews(self, slug, older_than=timedelta(days=7)):
        self.calls.append(("cleanup_previews",))
        return 0

    @contextmanager
    def hold_project_lock(self, slug):
        self.calls.append(("lock", slug))
        yield self.lock

    def load_project_modules(self, slug, include_off=False):
        self.calls.append(("load", slug))
        return [r for r in self.rows if include_off or r["mode"] != "off"]

    def fail_stale_runs(self, slug, older_than):
        self.calls.append(("fail_stale", older_than))
        return 0

    def last_run_started(self, slug, mkey, statuses=None):
        return (self.last_ok if statuses else self.last_started).get(mkey)

    def start_run(self, slug, mkey, mode, phase=None):
        rid = len(self.runs) + 1
        self.runs[rid] = {"key": mkey, "mode": mode, "phase": phase}
        self.calls.append(("start_run", mkey))
        return rid

    def finish_run(self, rid, status, summary, error=None):
        self.runs[rid].update(status=status, summary=summary, error=error)
        self.calls.append(("finish_run", self.runs[rid]["key"], status))

    def existing_findings(self, slug, mkey):
        return list(self.existing.get(mkey, []))

    def apply_reconciled(self, slug, mkey, run_id, rec):
        self.calls.append(("apply_reconciled", mkey))
        self.inserted.setdefault(mkey, []).extend(rec.insert)
        ids = list(range(100, 100 + len(rec.insert)))
        self.existing[mkey] = list(self.existing.get(mkey, [])) + [
            dataclasses.replace(_stored(i, "open", mkey, f.object_id), action=f.action,
                                details=f.details)
            for i, f in zip(ids, rec.insert)]
        return ids

    def approve_auto(self, slug, mkey, ids):
        # как store.approve_auto: предложение правила авторежим не одобряет
        self.calls.append(("approve_auto", mkey))
        by_id = {f.id: f for f in self.existing.get(mkey, [])}
        return [i for i in ids if not (i in by_id and by_id[i].details.get("propose"))]

    def pending(self, slug):
        return list(self._pending)

    def set_outcome(self, fid, status, result, rollback=False):
        self.outcomes.append((fid, status, rollback))
        return self.outcome_ok


class FakeModule:
    def __init__(self, mkey, findings=(), crash=False, trace=None, scanned=None):
        self.KEY, self.findings, self.crash = mkey, list(findings), crash
        self.trace = trace if trace is not None else []
        self.scanned = scanned
        self.applied: List[int] = []
        self.rolled: List[int] = []

    def run(self, ctx: Context) -> RunOutput:
        self.trace.append(("run", self.KEY))
        if self.crash:
            raise RuntimeError("boom")
        return RunOutput(findings=self.findings, summary={"x": 1}, scanned_logins=self.scanned)

    def apply(self, ctx, items):
        self.trace.append(("apply", self.KEY))
        self.applied += [i.id for i in items]
        return {i.id: ApplyResult(True, {}) for i in items}

    def rollback(self, ctx, items):
        self.trace.append(("rollback", self.KEY))
        self.rolled += [i.id for i in items]
        return {i.id: ApplyResult(True, {}) for i in items}


def _row(mkey="placements", mode="approve", **kw):
    return {"module_key": mkey, "mode": mode, "settings": {}, "run_requested_at": None, **kw}


def _go(store, modules, **kw):
    kw.setdefault("agent_lock_fn", lambda slug: None)
    return runner.run_project("lime", store=store, accounts=lambda s: (ACC, []),
                              modules=modules, now=NOW, log=lambda m: None, **kw)


def test_lock_taken_exits_zero():
    st = FakeStore([_row()], lock=False)
    m = FakeModule("placements")
    assert _go(st, {"placements": m}) == 0
    assert m.trace == [] and ("load", "lime") not in st.calls


def test_apply_phase_before_modules():
    trace = []
    m = FakeModule("placements", trace=trace)
    st = FakeStore([_row()], pending=[_stored(1, "approved")])
    assert _go(st, {"placements": m}) == 0
    assert trace == [("apply", "placements"), ("run", "placements")]
    assert st.outcomes == [(1, "applied", False)]


def test_rollback_requested_goes_to_rollback():
    m = FakeModule("placements")
    st = FakeStore([_row()], pending=[_stored(2, "rollback_requested")])
    _go(st, {"placements": m})
    assert m.rolled == [2] and m.applied == []
    assert st.outcomes == [(2, "rolled_back", True)]


def test_pending_of_report_module_untouched():
    m = FakeModule("placements")
    st = FakeStore([_row(mode="report")], pending=[_stored(1, "approved")])
    _go(st, {"placements": m})
    assert m.applied == [] and st.outcomes == []


def test_outcome_false_does_not_fail():
    m = FakeModule("placements")
    st = FakeStore([_row()], pending=[_stored(1, "approved")])
    st.outcome_ok = False
    assert _go(st, {"placements": m}) == 0


def test_auto_mode_applies_new_findings():
    m = FakeModule("placements", findings=[_finding("a.ru"), _finding("b.ru", action=False)])
    st = FakeStore([_row(mode="auto")])
    assert _go(st, {"placements": m}) == 0
    assert m.applied == [100]
    assert st.outcomes == [(100, "applied", False)]


def test_report_and_approve_never_apply_new():
    for mode in ("report", "approve"):
        m = FakeModule("placements", findings=[_finding()])
        st = FakeStore([_row(mode=mode)])
        _go(st, {"placements": m})
        assert m.applied == [] and st.outcomes == []
        assert any(c[0] == "apply_reconciled" for c in st.calls)


def test_agent_lock_blocks_writes_for_edu():
    m = FakeModule("placements", findings=[_finding()])
    st = FakeStore([_row(mode="auto")],
                   pending=[_stored(1, "approved"), _stored(2, "rollback_requested")])
    _go(st, {"placements": m}, agent_lock_fn=lambda slug: "агент EDU включён — запись у него")
    assert m.applied == [] and m.rolled == [] and st.outcomes == []
    assert any(c[0] == "apply_reconciled" for c in st.calls)
    summary = next(r["summary"] for r in st.runs.values())
    assert "агент EDU включён" in summary["agent_lock"]


def test_module_crash_isolated():
    trace = []
    bad = FakeModule("placements", crash=True, trace=trace)
    good = FakeModule("queries", trace=trace)
    st = FakeStore([_row("placements"), _row("queries")])
    assert _go(st, {"placements": bad, "queries": good}) == 1
    runs = {r["key"]: r for r in st.runs.values()}
    assert runs["placements"]["status"] == "error" and "boom" in runs["placements"]["error"]
    assert runs["queries"]["status"] == "ok"
    # упавший модуль ничего не закрывает
    assert ("apply_reconciled", "placements") not in st.calls


def test_no_accounts_skipped():
    m = FakeModule("placements")
    st = FakeStore([_row()])
    rc = runner.run_project("lime", store=st, accounts=lambda s: ([], ["нет токена"]),
                            modules={"placements": m}, now=NOW, log=lambda x: None,
                            agent_lock_fn=lambda s: None)
    assert rc == 0 and m.trace == []
    run = st.runs[1]
    assert run["status"] == "skipped" and "нет токена" in run["summary"]["reason"]


def test_unknown_module_skipped_not_crash():
    st = FakeStore([_row("ghost")])
    assert _go(st, {}) == 0
    assert st.runs[1]["status"] == "skipped"


def test_scanned_logins_default_from_ctx_accounts(monkeypatch):
    seen = []
    orig = runner.reconcile

    def spy(mkey, existing, found, now, scanned=None, **kw):
        seen.append(scanned)
        return orig(mkey, existing, found, now, scanned, **kw)

    monkeypatch.setattr(runner, "reconcile", spy)
    _go(FakeStore([_row()]), {"placements": FakeModule("placements")})
    _go(FakeStore([_row()]), {"placements": FakeModule("placements", scanned=["z"])})
    assert seen == [["login1"], ["z"]]


def test_not_due_module_not_run():
    m = FakeModule("queries")
    st = FakeStore([_row("queries")])
    st.last_started["queries"] = st.last_ok["queries"] = NOW - timedelta(hours=1)
    _go(st, {"queries": m})
    assert m.trace == [] and st.runs == {}


def test_error_run_retried_after_two_hours():
    m = FakeModule("queries")
    st = FakeStore([_row("queries")])
    st.last_started["queries"] = NOW - timedelta(hours=3)  # упавший прогон, ok сегодня не было
    _go(st, {"queries": m})
    assert m.trace == [("run", "queries")]


class BadApply(FakeModule):
    def apply(self, ctx, items):
        self.trace.append(("apply", self.KEY))
        raise RuntimeError("direct 500")


class BadRollback(FakeModule):
    def rollback(self, ctx, items):
        raise RuntimeError("direct 500")


class NoneApply(FakeModule):
    def apply(self, ctx, items):
        return None


def test_apply_raises_isolated_in_apply_phase():
    trace = []
    bad = BadApply("placements", trace=trace)
    good = FakeModule("queries", trace=trace)
    st = FakeStore([_row("placements"), _row("queries")], pending=[_stored(1, "approved")])
    assert _go(st, {"placements": bad, "queries": good}) == 1
    assert st.outcomes == [(1, "failed", False)]
    assert ("run", "queries") in trace


def test_apply_returning_none_same_as_raise():
    st = FakeStore([_row()], pending=[_stored(1, "approved")])
    assert _go(st, {"placements": NoneApply("placements")}) == 1
    assert st.outcomes == [(1, "failed", False)]


def test_rollback_raises_returns_to_applied():
    st = FakeStore([_row()], pending=[_stored(2, "rollback_requested")])
    assert _go(st, {"placements": BadRollback("placements")}) == 1
    assert st.outcomes == [(2, "applied", True)]


def test_auto_apply_raises_marks_failed_and_rc1():
    m = BadApply("placements", findings=[_finding("a.ru")])
    st = FakeStore([_row(mode="auto")])
    assert _go(st, {"placements": m}) == 1
    assert st.outcomes == [(100, "failed", False)]
    assert st.runs[1]["status"] == "ok"


def test_auto_applies_open_findings_recorded_earlier():
    # находка записана под замком агента или до перехода в auto: в этом прогоне она не новая
    old = dataclasses.replace(_stored(7, "open"), fingerprint="placements:r:login1:1:site.ru")
    m = FakeModule("placements", findings=[_finding("site.ru")])
    st = FakeStore([_row(mode="auto")], existing={"placements": [old]})
    assert _go(st, {"placements": m}) == 0
    assert m.applied == [7] and st.outcomes == [(7, "applied", False)]


def test_auto_does_not_apply_unconfirmed_open_finding():
    stale = dataclasses.replace(_stored(7, "open", obj="stale.ru"))
    m = FakeModule("placements", findings=[_finding("new.ru")])
    st = FakeStore([_row(mode="auto")], existing={"placements": [stale]})
    assert _go(st, {"placements": m}) == 0
    assert m.applied == [100] and st.outcomes == [(100, "applied", False)]


def test_only_off_module_logged_and_busy_message():
    logs = []
    st = FakeStore([])
    runner.run_project("lime", store=st, accounts=lambda s: (ACC, []), modules={},
                       now=NOW, only="queries", log=logs.append, agent_lock_fn=lambda s: None)
    assert any("queries выключен" in l for l in logs)
    logs.clear()
    runner.run_project("lime", store=FakeStore([], lock=False), accounts=lambda s: (ACC, []),
                       modules={}, now=NOW, log=logs.append, agent_lock_fn=lambda s: None)
    assert any("уже идёт" in l for l in logs)


def test_pending_without_module_is_logged():
    logs = []
    st = FakeStore([_row()], pending=[_stored(1, "approved")])
    runner.run_project("lime", store=st, accounts=lambda s: (ACC, []), modules={},
                       now=NOW, log=logs.append, agent_lock_fn=lambda s: None)
    assert any("ждёт применения" in l for l in logs)


def test_only_filters_modules():
    a, b = FakeModule("placements"), FakeModule("queries")
    st = FakeStore([_row("placements"), _row("queries")])
    _go(st, {"placements": a, "queries": b}, only="queries")
    assert a.trace == [] and b.trace == [("run", "queries")]


def test_is_due():
    msk = lambda d, h, mi=0: datetime(2026, 10, d, h, mi, tzinfo=timezone(timedelta(hours=3)))
    utc = lambda d, h, mi=0: datetime(2026, 10, d, h, mi, tzinfo=timezone.utc)
    now = msk(18, 12)
    due = runner.is_due  # (ключ, последний ok/skipped, последняя попытка, запрос, сейчас)
    # placements всегда
    assert due("placements", now, now, None, now)
    # queries: прогона не было вообще
    assert due("queries", None, None, None, now)
    # queries: ok сегодня по МСК (04:00 UTC = 07:00 МСК)
    assert not due("queries", utc(18, 4), utc(18, 4), None, now)
    # 21:30 UTC 17-го = 00:30 МСК 18-го — это уже сегодня
    assert not due("audit", utc(17, 21, 30), utc(17, 21, 30), None, now)
    # 20:00 UTC 17-го = 23:00 МСК 17-го — вчера
    assert due("audit", utc(17, 20), utc(17, 20), None, now)
    # до 07:00 МСК не стартуем
    assert not due("queries", utc(17, 10), utc(17, 10), None, msk(18, 6, 59))
    # ручной запуск перебивает расписание
    assert due("queries", utc(18, 4), utc(18, 4), utc(18, 5), now)
    # после error сутки не закрыты: повтор не раньше чем через 2 часа
    assert not due("queries", None, msk(18, 11), None, now)
    assert due("queries", None, msk(18, 9, 59), None, now)
    assert due("queries", utc(17, 4), msk(18, 9, 59), None, now)


def test_agent_lock():
    assert "агент EDU" in runner.agent_lock("edunetwork", read_autonomy=lambda: "full")
    assert runner.agent_lock("edunetwork", read_autonomy=lambda: "off") is None
    assert runner.agent_lock("lime", read_autonomy=lambda: "full") is None

    def boom():
        raise RuntimeError("db down")
    assert runner.agent_lock("edunetwork", read_autonomy=boom) is not None


def _dry(st, m, **kw):
    logs = []
    rc = runner.run_project("lime", store=st, accounts=lambda s: (ACC, []),
                            modules=m, now=NOW, dry_run=True, log=logs.append,
                            agent_lock_fn=lambda s: None, **kw)
    return rc, logs


def test_dry_run_reads_settings_writes_nothing():
    m = FakeModule("placements", findings=[_finding("a.ru")])
    off = FakeModule("queries")
    st = FakeStore([_row(mode="auto", settings={"top_n": 5})],
                   pending=[_stored(1, "approved")])
    seen = []
    orig = m.run
    m.run = lambda ctx: (seen.append((ctx.mode, ctx.settings["top_n"])), orig(ctx))[1]
    rc, logs = _dry(st, {"placements": m, "queries": off})
    assert rc == 0
    assert st.calls == [("load", "lime")] and st.outcomes == [] and m.applied == []
    assert seen == [("report", 5)] and off.trace == []
    assert any("a.ru" in line for line in logs)


def test_dry_run_explicit_module_even_if_off():
    q = FakeModule("queries")
    st = FakeStore([_row("placements")])
    _dry(st, {"queries": q}, only="queries")
    assert q.trace == [("run", "queries")]


def test_dry_run_db_read_fails_falls_back_to_defaults():
    m = FakeModule("placements")
    st = FakeStore([])

    def boom(slug):
        raise RuntimeError("db down")
    st.load_project_modules = boom
    rc, logs = _dry(st, {"placements": m})
    assert rc == 0 and m.trace == [("run", "placements")]
    assert any("предупреждение" in line for line in logs)


def test_unverified_reaches_reconcile_and_summary(monkeypatch):
    seen = []
    orig = runner.reconcile

    def spy(mkey, existing, found, now, scanned=None, **kw):
        seen.append(kw.get("unverified"))
        return orig(mkey, existing, found, now, scanned, **kw)

    monkeypatch.setattr(runner, "reconcile", spy)
    m = FakeModule("audit")
    m.run = lambda ctx: RunOutput(findings=[], unverified=[("login1", "ads_rejected")])
    st = FakeStore([_row("audit", mode="report")])
    _go(st, {"audit": m})
    assert seen == [[("login1", "ads_rejected")]]
    assert st.runs[1]["summary"]["unverified"] == {"login1": ["ads_rejected"]}


# --- фаза применения пишет свою строку module_runs (I2) ---

def _apply_rows(st):
    return [r for r in st.runs.values() if r["phase"] == "apply"]


def test_apply_recorded_even_when_scan_not_due():
    m = FakeModule("queries")
    st = FakeStore([_row("queries")], pending=[_stored(1, "approved", key="queries")])
    st.last_started["queries"] = st.last_ok["queries"] = NOW - timedelta(hours=1)
    assert _go(st, {"queries": m}) == 0
    assert m.trace == [("apply", "queries")]  # скан не due, применение было
    (row,) = st.runs.values()
    assert row["phase"] == "apply" and row["status"] == "ok"
    assert row["summary"] == {"phase": "apply", "apply": {"applied": 1}}


def test_apply_errors_do_not_clobber_module_errors():
    class ErrScan(BadApply):
        def run(self, ctx):
            return RunOutput(findings=[], summary={"errors": ["login2: Директ лёг"]})

    st = FakeStore([_row()], pending=[_stored(1, "approved")])
    assert _go(st, {"placements": ErrScan("placements")}) == 1
    (apply_row,) = _apply_rows(st)
    assert apply_row["status"] == "error" and "direct 500" in apply_row["error"]
    assert apply_row["summary"]["apply"] == {"apply_errors": 1, "failed": 1}
    scan = next(r for r in st.runs.values() if r["phase"] is None)
    assert scan["summary"]["errors"] == ["login2: Директ лёг"]
    assert "applied" not in scan["summary"] and "failed" not in scan["summary"]


def test_no_work_no_apply_row():
    st = FakeStore([_row(mode="report")], pending=[_stored(1, "approved")])
    _go(st, {"placements": FakeModule("placements")})
    assert _apply_rows(st) == []


def test_skipped_changed_counted_in_apply_row():
    st = FakeStore([_row()], pending=[_stored(2, "rollback_requested")])
    st.outcome_ok = False
    _go(st, {"placements": FakeModule("placements")})
    (row,) = _apply_rows(st)
    assert row["summary"]["apply"] == {"skipped_changed": 1}


# --- откат в report выполняется, одобренное — нет; off не трогается (I3) ---

def test_rollback_processed_in_report_but_approved_not_applied():
    m = FakeModule("placements")
    st = FakeStore([_row(mode="report")],
                   pending=[_stored(1, "approved"), _stored(2, "rollback_requested", obj="b.ru")])
    _go(st, {"placements": m})
    assert m.rolled == [2] and m.applied == []
    assert st.outcomes == [(2, "rolled_back", True)]
    (row,) = _apply_rows(st)
    assert row["mode"] == "report" and row["summary"]["apply"] == {"rolled_back": 1}


def test_nothing_processed_in_off():
    m = FakeModule("placements")
    st = FakeStore([_row(mode="off")],
                   pending=[_stored(1, "approved"), _stored(2, "rollback_requested", obj="b.ru")])
    _go(st, {"placements": m})
    assert m.trace == [] and st.outcomes == [] and st.runs == {}


def test_rollback_in_report_still_respects_agent_lock():
    m = FakeModule("placements")
    st = FakeStore([_row(mode="report")], pending=[_stored(2, "rollback_requested")])
    _go(st, {"placements": m}, agent_lock_fn=lambda slug: "агент EDU включён — запись у него")
    assert m.rolled == [] and st.outcomes == []


# --- убитый прогон закрывается при следующем старте проекта (M-e) ---

def test_stale_runs_failed_under_lock_before_work():
    st = FakeStore([_row()])
    _go(st, {"placements": FakeModule("placements")})
    assert st.calls[:2] == [("lock", "lime"), ("fail_stale", timedelta(minutes=80))]
    st = FakeStore([_row()], lock=False)
    _go(st, {"placements": FakeModule("placements")})
    assert not any(c[0] == "fail_stale" for c in st.calls)


# --- репетиция выключенного модуля берёт его сохранённые настройки (M-f) ---

def test_dry_run_off_module_uses_saved_settings():
    q = FakeModule("queries")
    seen = []
    orig = q.run
    q.run = lambda ctx: (seen.append(ctx.settings["max_per_run"]), orig(ctx))[1]
    st = FakeStore([_row("queries", mode="off", settings={"max_per_run": 17})])
    rc, _logs = _dry(st, {"queries": q}, only="queries")
    assert rc == 0 and seen == [17]
    # без only выключенный модуль не гоняется
    q2 = FakeModule("queries")
    _dry(st, {"queries": q2})
    assert q2.trace == []


# --- пробный прогон (превью) ---

def _preview(pid=1, mkey="placements", settings=None, rule_id=None):
    return {"id": pid, "dashboard_slug": "lime", "module_key": mkey,
            "settings": settings if settings is not None else {}, "rule_id": rule_id,
            "status": "queued"}


def _uf(obj, rule="u:aaa111", login="login1", cid=1, clicks=1, cost=10.0, **details):
    return Finding(rule=rule, severity="info", title="t", login=login, campaign_id=cid,
                   campaign_name="К%d" % cid, object_type="site", object_id=obj,
                   details={"clicks": clicks, "cost": cost,
                            "metrics": {"clicks": clicks, "cost": cost, "leads": 0},
                            **details},
                   action={"op": "exclude_site", "campaign_id": cid, "site": obj})


class OutModule(FakeModule):
    """Модуль, отдающий готовый RunOutput: и скану, и превью — одно и то же."""

    def __init__(self, mkey, out, **kw):
        super().__init__(mkey, **kw)
        self.out = out
        self.modes: List[str] = []

    def run(self, ctx):
        self.trace.append(("run", ctx.module_key))
        self.modes.append(ctx.mode)
        ctx.progress({"step": "login1", "logins_done": 1, "logins_total": 1})
        return self.out


def _done(st, pid=1):
    p = st._preview(pid)
    assert p["status"] == "done", p
    return p["result"]


def test_preview_equals_scan_on_same_fixtures(monkeypatch):
    # общий тест «превью = скан»: настоящий модуль площадок на одних и тех же отчётах
    from sync.modules import placements as pm
    from sync.placements import direct
    monkeypatch.delenv(pm.LLM_ENV, raising=False)
    camps = [{"Id": c, "Name": "К%d" % c, "Type": "TEXT_CAMPAIGN", "State": "ON",
              "ExcludedSites": {"Items": []}} for c in (1, 2)]
    rows = [{"campaign_id": str(c), "placement": s, "clicks": 10 + i, "cost": 5.0 + i,
             "impressions": 100}
            for c in (1, 2) for i, s in enumerate(["com.aaa.bbb", "plain.example.ru"])]
    monkeypatch.setattr(direct, "campaigns", lambda t, l: camps)
    monkeypatch.setattr(direct, "placements_today", lambda t, l: rows)
    monkeypatch.setattr(direct, "campaign_goals", lambda t, l: {})
    monkeypatch.setattr(direct, "placements_with_goals", lambda t, l, g: [])
    monkeypatch.setattr(direct, "placements_window", lambda t, l, d, g: [])
    settings = {"rules": [{"id": "aaa111", "name": "Клики", "enabled": True, "action": "block",
                           "window": 0, "where": "seen",
                           "campaigns": {"only": [], "except": []},
                           "conditions": [{"field": "clicks", "op": "gte", "value": 1}]}]}
    st = FakeStore([_row(mode="report", settings=settings)],
                   previews=[_preview(settings=settings)])
    assert _go(st, {"placements": pm}) == 0
    res = _done(st)
    scan = sorted((f.login, f.campaign_id, f.object_id, f.rule)
                  for f in st.inserted["placements"])
    would = sorted((w["login"], w["campaign_id"], w["object"], w["rule"]) for w in res["would"])
    assert scan and would == scan
    assert {x[3] for x in scan} == {"app", "u:aaa111"}
    assert res["totals"]["would"] == len(scan)
    assert st._preview(1)["progress"][-1] == {"step": "login1", "logins_done": 1,
                                              "logins_total": 1}


def test_preview_runs_in_report_and_writes_nothing():
    m = OutModule("placements", RunOutput(findings=[_uf("a.ru")]))
    st = FakeStore([], previews=[_preview()])
    assert _go(st, {"placements": m}) == 0
    res = _done(st)
    assert m.modes == ["report"] and m.applied == [] and st.outcomes == []
    banned = {"apply_reconciled", "set_outcome", "approve_auto", "start_run"}
    assert not [c for c in st.calls if c[0] in banned]
    assert res["would"] == [{"object": "a.ru", "campaign_id": 1, "campaign_name": "К1",
                             "login": "login1", "rule": "u:aaa111", "currency": "RUB",
                             "metrics": {"clicks": 1, "cost": 10.0, "leads": 0},
                             "action": {"op": "exclude_site", "campaign_id": 1,
                                        "site": "a.ru"}}]
    assert res["totals"] == {"would": 1, "clicks": 1, "cost": 10.0, "refused": 0}
    assert ("cleanup_previews",) in st.calls


def test_preview_taken_when_lock_busy():
    m = OutModule("placements", RunOutput(findings=[_uf("a.ru")]))
    logs = []
    st = FakeStore([_row()], lock=False, previews=[_preview()])
    rc = runner.run_project("lime", store=st, accounts=lambda s: (ACC, []),
                            modules={"placements": m}, now=NOW, log=logs.append,
                            agent_lock_fn=lambda s: None)
    assert rc == 0 and any("уже идёт" in line for line in logs)
    assert _done(st)["totals"]["would"] == 1
    assert not any(c[0] in ("load", "start_run", "fail_stale") for c in st.calls)


def test_lock_busy_without_previews_does_not_read_accounts():
    asked = []
    st = FakeStore([_row()], lock=False)
    runner.run_project("lime", store=st, accounts=lambda s: asked.append(s) or (ACC, []),
                       modules={}, now=NOW, log=lambda m: None, agent_lock_fn=lambda s: None)
    # кабинеты EDU читаются через Директ: без заявки на превью дёргать их незачем
    assert asked == []


def test_preview_taken_between_modules_and_at_end():
    st = FakeStore([_row("placements"), _row("queries")])

    class Enqueue(OutModule):
        def __init__(self, mkey, out, pid, target):
            super().__init__(mkey, out)
            self.pid, self.target = pid, target

        def run(self, ctx):
            if ctx.mode != "report":
                st.previews.append(_preview(self.pid, self.target))
            return super().run(ctx)

    p = Enqueue("placements", RunOutput(findings=[]), 1, "queries")
    q = Enqueue("queries", RunOutput(findings=[_uf("слово", rule="phrase_waste")]), 2,
                "placements")
    assert _go(st, {"placements": p, "queries": q}) == 0
    order = [c for c in st.calls if c[0] in ("finish_run", "finish_preview")]
    assert order == [("finish_run", "placements", "ok"), ("finish_preview", 1, "done"),
                     ("finish_run", "queries", "ok"), ("finish_preview", 2, "done")]
    assert [w["object"] for w in _done(st, 1)["would"]] == ["слово"]


def test_preview_rule_id_filters_would_only():
    out = RunOutput(findings=[_uf("a.ru"), _uf("b.ru", rule="app"), _uf("c.ru", rule="u:bbb222")],
                    summary={"by_rule": {"u:aaa111": {"found": 1, "refused": 2, "over_cap": 3},
                                         "app": {"found": 1, "refused": 0, "over_cap": 0}},
                             "refused": [{"code": "list_full", "rule": "u:bbb222"}]})
    st = FakeStore([], previews=[_preview(rule_id="aaa111")])
    _go(st, {"placements": OutModule("placements", out)})
    res = _done(st)
    assert [w["object"] for w in res["would"]] == ["a.ru"]
    assert res["totals"]["would"] == 1
    assert res["by_rule"]["u:aaa111"] == {"would": 1, "refused": 2, "over_cap": 3}
    assert res["by_rule"]["app"] == {"would": 1, "refused": 0, "over_cap": 0}
    assert res["refused"] == [{"code": "list_full", "rule": "u:bbb222"}]


HELD = {"code": "goal_not_visible", "object": None, "campaign_id": None, "rule": None,
        "login": "login1", "held": True, "by": "u:ppp111"}


def test_preview_truncates_with_full_totals_and_currency():
    kz = DirectAccount("kz1", "tok", "KZT", "X")
    findings = [_uf("s%03d.ru" % i, clicks=1, cost=1.0) for i in range(550)]
    findings += [_uf("kz%d.ru" % i, login="kz1", clicks=2, cost=100.0) for i in range(3)]
    findings += [_uf("top.ru", clicks=1, cost=50.0)]
    refused = [{"code": "list_full", "rule": "u:aaa111", "object": str(i)} for i in range(350)]
    out = RunOutput(findings=findings,
                    summary={"refused": ["строка этапа 1"] + refused + [HELD]})
    st = FakeStore([], previews=[_preview()])
    runner.run_project("lime", store=st, accounts=lambda s: (ACC + [kz], []),
                       modules={"placements": OutModule("placements", out)}, now=NOW,
                       log=lambda m: None, agent_lock_fn=lambda s: None)
    res = _done(st)
    assert len(res["would"]) == 500 and len(res["refused"]) == 300
    assert res["totals"] == {"would": 554, "clicks": 557, "cost": 600.0, "refused": 351,
                             "cost_other": {"KZT": 300.0}}
    # курса нет: рубли первыми по убыванию расхода, обрезка режет сначала чужую валюту
    assert res["would"][0]["object"] == "top.ru"
    assert {w["currency"] for w in res["would"]} == {"RUB"}
    # удержание защитой не теряется за обычными отказами
    assert res["refused"][0] == HELD
    assert res["refused_by_code"] == {"list_full": 350, "goal_not_visible": 1}
    assert all(isinstance(r, dict) for r in res["refused"])
    assert res["refused_lines"] == ["строка этапа 1"]
    assert res["logins_total"] == 2
    assert res["currency_by_login"] == {"kz1": "KZT"}


def test_preview_no_currency_by_login_when_all_rub():
    out = RunOutput(findings=[_uf("a.ru")])
    st = FakeStore([], previews=[_preview()])
    _go(st, {"placements": OutModule("placements", out)})
    assert "currency_by_login" not in _done(st)


def test_preview_would_carries_family_fields():
    f = _uf("слово", rule="phrase_waste", metrics_scope="family", family_queries=3,
            broad={"queries": 4, "leads": 21, "clicks": 59})
    out = RunOutput(findings=[f, _uf("b.ru")])
    st = FakeStore([], previews=[_preview(mkey="queries")])
    _go(st, {"queries": OutModule("queries", out)})
    w = {x["object"]: x for x in _done(st)["would"]}
    assert (w["слово"]["metrics_scope"], w["слово"]["family_queries"]) == ("family", 3)
    assert w["слово"]["broad"] == {"queries": 4, "leads": 21, "clicks": 59}
    assert not {"metrics_scope", "family_queries", "broad"} & w["b.ru"].keys()


def test_preview_unverified_errors_skipped_shape():
    out = RunOutput(findings=[], unverified=[("login1", "u:aaa111"), ("login1", "u:bbb222", 5),
                                              ("login1", "spam")],
                    summary={"errors": ["login1: отчёт не готов"], "skipped": ["login1: off"],
                             "disabled": ["spam"]})
    st = FakeStore([], previews=[_preview()])
    _go(st, {"placements": OutModule("placements", out)})
    res = _done(st)
    # выключенное настройкой — не «не проверено»: оно лежит в disabled
    assert res["unverified"] == {"login1": ["u:aaa111", ["u:bbb222", 5]]}
    assert res["disabled"] == ["spam"]
    # та же форма, что summary.errors скана: Panda разбирает обе одним errorLogins
    assert res["errors"] == ["login1: отчёт не готов"]
    assert res["skipped"] == ["login1: off"]
    assert res["totals"] == {"would": 0, "clicks": 0, "cost": 0.0, "refused": 0}


def test_preview_crash_is_error_and_next_preview_runs():
    st = FakeStore([], previews=[_preview(1), _preview(2, "queries")])
    good = OutModule("queries", RunOutput(findings=[]))
    _go(st, {"placements": FakeModule("placements", crash=True), "queries": good})
    p1 = st._preview(1)
    assert p1["status"] == "error" and "boom" in p1["error"] and p1["result"] is None
    assert st._preview(2)["status"] == "done"


def test_preview_without_accounts_or_module_is_error():
    st = FakeStore([], previews=[_preview(1), _preview(2, "queries")])
    runner.run_project("lime", store=st, accounts=lambda s: ([], ["нет токена"]),
                       modules={"placements": FakeModule("placements")}, now=NOW,
                       log=lambda m: None, agent_lock_fn=lambda s: None)
    assert "нет токена" in st._preview(1)["error"]
    assert "не реализован" in st._preview(2)["error"]


def test_preview_cleanup_failure_not_fatal():
    st = FakeStore([], previews=[_preview()])

    def boom(slug, older_than=None):
        raise RuntimeError("db down")
    st.cleanup_previews = boom
    logs = []
    runner.run_project("lime", store=st, accounts=lambda s: (ACC, []),
                       modules={"placements": OutModule("placements", RunOutput(findings=[]))},
                       now=NOW, log=logs.append, agent_lock_fn=lambda s: None)
    assert st._preview(1)["status"] == "done"
    assert any("db down" in line for line in logs)


def test_preview_gets_project_goals_and_preview_settings():
    seen = []
    m = OutModule("placements", RunOutput(findings=[]))
    orig = m.run
    m.run = lambda ctx: (seen.append((ctx.project_goals, ctx.settings["max_per_run"])),
                         orig(ctx))[1]
    st = FakeStore([], previews=[_preview(settings={"max_per_run": 7})], goals=[30, 40])
    _go(st, {"placements": m})
    assert seen == [([30, 40], 7)]


def test_run_preview_by_id_runs_only_that_preview():
    m = OutModule("placements", RunOutput(findings=[_uf("a.ru")]))
    st = FakeStore([_row()], previews=[_preview(1), _preview(2)])
    rc = runner.run_preview("lime", 2, store=st, accounts=lambda s: (ACC, []),
                            modules={"placements": m}, now=NOW, log=lambda x: None)
    assert rc == 0 and st._preview(2)["status"] == "done"
    assert st._preview(1)["status"] == "queued" and st.runs == {}
    assert runner.run_preview("lime", 9, store=st, accounts=lambda s: (ACC, []),
                              modules={"placements": m}, now=NOW, log=lambda x: None) == 1


# --- сводка скана: тройки, предел отказов, кабинеты, валюта ---

def test_scan_summary_triples_refused_cap_logins_currency():
    kz = DirectAccount("kz1", "tok", "KZT", "X")
    refused = ["строка этапа 1"] + [{"code": "list_full", "rule": "u:aaa111"}] * 349 + [HELD]
    out = RunOutput(findings=[_uf("a.ru"), _uf("k.ru", login="kz1")],
                    summary={"refused": refused},
                    unverified=[("login1", "u:aaa111"), ("login1", "u:bbb222", 5),
                                ("kz1", "u:bbb222", 6)])
    st = FakeStore([_row(mode="report")])
    rc = runner.run_project("lime", store=st, accounts=lambda s: (ACC + [kz], []),
                            modules={"placements": OutModule("placements", out)}, now=NOW,
                            log=lambda m: None, agent_lock_fn=lambda s: None)
    assert rc == 0 and st.runs[1]["status"] == "ok"
    s = st.runs[1]["summary"]
    assert s["unverified"] == {"login1": ["u:aaa111", ["u:bbb222", 5]], "kz1": [["u:bbb222", 6]]}
    assert len(s["refused"]) == 300 and s["refused_total"] == 351
    # удержание поднято наверх, остальной порядок модуля сохранён
    assert s["refused"][:2] == [HELD, "строка этапа 1"]
    assert s["refused_by_code"] == {"list_full": 349, "goal_not_visible": 1}
    assert s["logins_total"] == 2
    assert s["currency_by_login"] == {"kz1": "KZT"}
    by_obj = {f.object_id: f for f in st.inserted["placements"]}
    assert by_obj["k.ru"].details["currency"] == "KZT"
    assert "currency" not in by_obj["a.ru"].details


def test_scan_all_rub_has_no_currency_map():
    st = FakeStore([_row(mode="report")])
    _go(st, {"placements": OutModule("placements", RunOutput(findings=[_uf("a.ru")]))})
    s = st.runs[1]["summary"]
    assert "currency_by_login" not in s and s["logins_total"] == 1


def test_scan_triple_holds_only_its_campaign_finding():
    old = NOW - timedelta(hours=100)
    mine = dataclasses.replace(_stored(7, "open", obj="a.ru"), rule="u:aaa111",
                               fingerprint="placements:u:aaa111:login1:1:a.ru", last_seen_at=old)
    other = dataclasses.replace(_stored(8, "open", obj="b.ru"), rule="u:aaa111", campaign_id=2,
                                fingerprint="placements:u:aaa111:login1:2:b.ru", last_seen_at=old)
    out = RunOutput(findings=[], unverified=[("login1", "u:aaa111", 1)])
    expired = []
    st = FakeStore([_row(mode="report")], existing={"placements": [mine, other]})
    orig = st.apply_reconciled
    st.apply_reconciled = lambda *a: (expired.extend(a[3].expire), orig(*a))[1]
    _go(st, {"placements": OutModule("placements", out)})
    assert expired == [8]
    assert st.runs[1]["summary"]["expired"] == 1


def test_auto_holds_propose_findings():
    m = OutModule("placements", RunOutput(findings=[_uf("a.ru"), _uf("p.ru", propose=True)]))
    st = FakeStore([_row(mode="auto")])
    assert _go(st, {"placements": m}) == 0
    assert m.applied == [100]
    assert st.runs[1]["summary"]["auto_held"] == 1


def test_scan_ctx_gets_project_goals():
    seen = []
    m = OutModule("placements", RunOutput(findings=[]))
    orig = m.run
    m.run = lambda ctx: (seen.append(ctx.project_goals), orig(ctx))[1]
    _go(FakeStore([_row()], goals=[30]), {"placements": m})
    assert seen == [[30]]


def test_cli_preview_id_runs_preview_not_scan(monkeypatch):
    from sync import modules_run
    calls = []
    monkeypatch.setattr(modules_run, "run_preview",
                        lambda slug, pid, **kw: calls.append(("preview", slug, pid)) or 0)
    monkeypatch.setattr(modules_run, "run_project",
                        lambda slug, **kw: calls.append(("scan", slug)) or 0)
    assert modules_run.main(["--project", "lime", "--preview-id", "12"]) == 0
    assert modules_run.main(["--project", "lime"]) == 0
    assert calls == [("preview", "lime", 12), ("scan", "lime")]


# --- раунд правок 1 ---

def test_preview_filed_during_apply_taken_at_end_without_scans():
    st = FakeStore([_row("queries")], pending=[_stored(1, "approved", key="queries")])
    st.last_started["queries"] = st.last_ok["queries"] = NOW - timedelta(hours=1)

    class Q(OutModule):
        def apply(self, ctx, items):
            st.previews.append(_preview(5, "queries"))
            return super().apply(ctx, items)

    _go(st, {"queries": Q("queries", RunOutput(findings=[]))})
    assert _done(st, 5)["totals"]["would"] == 0
    # сканов не было: превью взял вызов в конце такта
    assert [r["phase"] for r in st.runs.values()] == ["apply"]


def _flaky_finish(st, fail_fallback=False):
    orig = st.finish_preview

    def finish(pid, status, result, error=None):
        if pid == 1 and (status == "done" or fail_fallback):
            raise ValueError("jsonb не принял NaN")
        return orig(pid, status, result, error=error)
    st.finish_preview = finish


def test_finish_preview_failure_falls_back_to_error_and_queue_goes_on():
    st = FakeStore([], previews=[_preview(1), _preview(2)])
    _flaky_finish(st)
    logs = []
    runner.run_project("lime", store=st, accounts=lambda s: (ACC, []),
                       modules={"placements": OutModule("placements", RunOutput(findings=[]))},
                       now=NOW, log=logs.append, agent_lock_fn=lambda s: None)
    p1 = st._preview(1)
    assert p1["status"] == "error" and p1["result"] is None
    assert p1["error"].startswith("не удалось сохранить результат: ") and "NaN" in p1["error"]
    assert st._preview(2)["status"] == "done"
    assert any("NaN" in line for line in logs)


def test_finish_preview_fallback_failure_still_continues_queue():
    st = FakeStore([], previews=[_preview(1), _preview(2)])
    _flaky_finish(st, fail_fallback=True)
    logs = []
    runner.run_project("lime", store=st, accounts=lambda s: (ACC, []),
                       modules={"placements": OutModule("placements", RunOutput(findings=[]))},
                       now=NOW, log=logs.append, agent_lock_fn=lambda s: None)
    # строка останется running и закроется как прерванная; соседнее превью не страдает
    assert st._preview(1)["status"] == "running"
    assert st._preview(2)["status"] == "done"
    assert any("не записан" in line for line in logs)


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def spend(self, minutes):
        self.t += minutes * 60


def _go_clock(st, modules, clock, **kw):
    return runner.run_project("lime", store=st, accounts=kw.pop("accounts", lambda s: (ACC, [])),
                              modules=modules, now=NOW, log=lambda m: None,
                              agent_lock_fn=lambda s: None, clock=clock, **kw)


def test_preview_budget_skips_between_modules_after_20_min():
    clock = _Clock()
    st = FakeStore([_row("placements"), _row("queries")])

    class Slow(OutModule):
        def run(self, ctx):
            if ctx.mode != "report":
                clock.spend(21)
                st.previews.append(_preview(1, "queries"))
            return super().run(ctx)

    _go_clock(st, {"placements": Slow("placements", RunOutput(findings=[])),
                   "queries": OutModule("queries", RunOutput(findings=[]))}, clock)
    order = [c for c in st.calls if c[0] in ("finish_run", "finish_preview")]
    # 21 минута такта прошла: между модулями не берём, в конце — без бюджета
    assert order == [("finish_run", "placements", "ok"), ("finish_run", "queries", "ok"),
                     ("finish_preview", 1, "done")]


def test_preview_budget_before_apply_and_inside_queue():
    clock = _Clock()
    st = FakeStore([_row("placements")], previews=[_preview(1)])

    def slow_accounts(slug):
        clock.spend(21)
        return ACC, []

    _go_clock(st, {"placements": OutModule("placements", RunOutput(findings=[]))}, clock,
              accounts=slow_accounts)
    order = [c for c in st.calls if c[0] in ("finish_run", "finish_preview")]
    assert order == [("finish_run", "placements", "ok"), ("finish_preview", 1, "done")]

    # внутри очереди бюджет проверяется перед каждым превью
    clock = _Clock()
    st = FakeStore([_row("placements")], previews=[_preview(1), _preview(2, "queries")])

    class SlowPreview(OutModule):
        def run(self, ctx):
            if ctx.mode == "report" and ctx.module_key == "placements":
                clock.spend(21)
            return super().run(ctx)

    _go_clock(st, {"placements": SlowPreview("placements", RunOutput(findings=[])),
                   "queries": OutModule("queries", RunOutput(findings=[]))}, clock)
    order = [c for c in st.calls if c[0] in ("finish_run", "finish_preview")]
    assert order == [("finish_preview", 1, "done"), ("finish_run", "placements", "ok"),
                     ("finish_preview", 2, "done")]


def test_preview_busy_lock_has_no_budget():
    clock = _Clock()
    clock.spend(60)
    st = FakeStore([_row()], lock=False, previews=[_preview()])
    _go_clock(st, {"placements": OutModule("placements", RunOutput(findings=[]))}, clock)
    assert _done(st)["totals"]["would"] == 0


def test_cleanup_previews_once_per_tick():
    st = FakeStore([_row("placements"), _row("queries")])
    _go(st, {"placements": OutModule("placements", RunOutput(findings=[])),
             "queries": OutModule("queries", RunOutput(findings=[]))})
    assert st.calls.count(("cleanup_previews",)) == 1
    st = FakeStore([_row()], lock=False)
    _go(st, {"placements": OutModule("placements", RunOutput(findings=[]))})
    assert st.calls.count(("cleanup_previews",)) == 1
