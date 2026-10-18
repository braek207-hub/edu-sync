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
    def __init__(self, rows, pending=(), lock=True, existing=None):
        self.rows, self._pending, self.lock = rows, list(pending), lock
        self.existing = existing or {}
        self.calls: List[tuple] = []
        self.outcomes: List[tuple] = []
        self.runs: Dict[int, dict] = {}
        self.last_started: Dict[str, Optional[datetime]] = {}
        self.last_ok: Dict[str, Optional[datetime]] = {}
        self.outcome_ok = True

    @contextmanager
    def hold_project_lock(self, slug):
        self.calls.append(("lock", slug))
        yield self.lock

    def load_project_modules(self, slug):
        self.calls.append(("load", slug))
        return self.rows

    def last_run_started(self, slug, mkey, statuses=None):
        return (self.last_ok if statuses else self.last_started).get(mkey)

    def start_run(self, slug, mkey, mode):
        rid = len(self.runs) + 1
        self.runs[rid] = {"key": mkey, "mode": mode}
        self.calls.append(("start_run", mkey))
        return rid

    def finish_run(self, rid, status, summary, error=None):
        self.runs[rid].update(status=status, summary=summary, error=error)
        self.calls.append(("finish_run", self.runs[rid]["key"], status))

    def existing_findings(self, slug, mkey):
        return list(self.existing.get(mkey, []))

    def apply_reconciled(self, slug, mkey, run_id, rec):
        self.calls.append(("apply_reconciled", mkey))
        ids = list(range(100, 100 + len(rec.insert)))
        self.existing[mkey] = list(self.existing.get(mkey, [])) + [
            dataclasses.replace(_stored(i, "open", mkey, f.object_id), action=f.action)
            for i, f in zip(ids, rec.insert)]
        return ids

    def approve_auto(self, slug, mkey, ids):
        self.calls.append(("approve_auto", mkey))
        return list(ids)

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

    def spy(mkey, existing, found, now, scanned=None):
        seen.append(scanned)
        return orig(mkey, existing, found, now, scanned)

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
