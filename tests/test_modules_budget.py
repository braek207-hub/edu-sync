# -*- coding: utf-8 -*-
"""Модуль budget: применение одобренной правки стратегии (недельный лимит, цель CPA) на
поддельном Директе, окно отмены 10 с и замок агента EDU в раннере."""

import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import pytest

from sync.modules import budget as bm
from sync.modules import runner
from sync.modules.accounts import DirectAccount
from sync.modules.defaults import ALLOWED_MODES, settings_for
from sync.modules.lifecycle import reconcile
from sync.modules.registry import MODULES
from sync.modules.types import ApplyResult, Context, StoredFinding

NOW = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)
ACC = [DirectAccount("edu-login", "tok", "RUB", "EDU")]
M = 1_000_000


def _strategy(limit_rub=20000, cpa_rub=1500):
    return {"Search": {"BiddingStrategyType": "AVERAGE_CPA",
                       "AverageCpa": {"AverageCpa": cpa_rub * M, "GoalId": 7,
                                      "WeeklySpendLimit": limit_rub * M,
                                      "BidCeiling": 300 * M}},
            "Network": {"BiddingStrategyType": "SERVING_OFF"}}


class FakeDirect:
    """campaigns.get/update одной кабинетной базы; update пишет стратегию как есть."""

    def __init__(self, monkeypatch, camps: Dict[int, dict], accept=True, write_error=None,
                 read_error_after_write=None, warnings=None):
        self.camps = camps
        self.accept = accept
        self.write_error = write_error
        self.read_error_after_write = read_error_after_write
        self.warnings = warnings
        self.updates: List[dict] = []
        self.item_errors: List[dict] = []
        self.gets = 0
        monkeypatch.setattr(bm.direct, "call", self.call)

    def call(self, token, login, service, payload):
        assert service == "campaigns"
        params = payload["params"]
        if payload["method"] == "get":
            self.gets += 1
            if self.updates and self.read_error_after_write is not None:
                raise self.read_error_after_write
            ids = params["SelectionCriteria"]["Ids"]
            out = []
            for cid in ids:
                c = self.camps.get(int(cid))
                if c is None:
                    continue  # мастер кампаний API не видит
                item = {"Id": cid, "Name": c.get("name", "EDU | Поиск | ВУЗ"),
                        "Type": c.get("type", "TEXT_CAMPAIGN"), "DailyBudget": None}
                if item["Type"] == "TEXT_CAMPAIGN":
                    tc = {"BiddingStrategy": copy.deepcopy(c["strategy"])}
                    if c.get("package"):
                        tc["PackageBiddingStrategy"] = {"StrategyId": c["package"]}
                    if c.get("goals"):
                        tc["PriorityGoals"] = {"Items": copy.deepcopy(c["goals"])}
                    item["TextCampaign"] = tc
                out.append(item)
            return {"Campaigns": out}
        assert payload["method"] == "update"
        self.updates.append(copy.deepcopy(params))
        row = params["Campaigns"][0]
        if self.accept:
            self.camps[int(row["Id"])]["strategy"] = copy.deepcopy(
                row["TextCampaign"]["BiddingStrategy"])
        if self.write_error is not None:
            raise self.write_error
        res = {"Id": row["Id"]}
        if self.item_errors:
            res["Errors"] = self.item_errors
        if self.warnings:
            res["Warnings"] = self.warnings
        return {"UpdateResults": [res]}


def _finding(fid=1, cid=101, expect=None, set_=None, status="approved", decided_at=None,
             response=None) -> StoredFinding:
    action = {"op": "set_strategy", "campaign_id": cid, "login": "edu-login",
              "expect": expect if expect is not None else {"weekly_budget": 20000,
                                                           "target_cpa": 1500},
              "set": set_ if set_ is not None else {"weekly_budget": 26000, "target_cpa": 1800}}
    return StoredFinding(id=fid, status=status, fingerprint="budget:add:edu-login:%d:%d" % (cid, cid),
                         last_seen_at=NOW, rule="add", severity="info", title="t",
                         login="edu-login", campaign_id=cid, campaign_name="c",
                         object_type="campaign", object_id=str(cid), details={}, action=action,
                         decided_at=decided_at or NOW - timedelta(minutes=1), response=response)


def _ctx(settings=None, accounts=None) -> Context:
    return Context(slug="edunetwork", module_key="budget", mode="approve",
                   settings=settings_for("budget", settings), accounts=accounts or ACC, now=NOW,
                   log=lambda m: None)


# --- модуль ---------------------------------------------------------------------------------

def test_registered_and_modes():
    assert MODULES["budget"] is bm and bm.KEY == "budget"
    assert ALLOWED_MODES["budget"] == ("off", "report", "approve")


def test_writes_limit_and_cpa(monkeypatch):
    fake = FakeDirect(monkeypatch, {101: {"strategy": _strategy()}})
    res = bm.apply(_ctx(), [_finding()])
    assert res[1].ok, res[1].error
    assert len(fake.updates) == 1
    sent = fake.updates[0]["Campaigns"][0]
    assert sent["Id"] == 101
    # стратегия целиком: соседние поля, которые ставил человек, на месте
    want = _strategy(limit_rub=26000, cpa_rub=1800)
    assert sent["TextCampaign"]["BiddingStrategy"] == want
    assert res[1].response == {"before": {"weekly_budget": 20000, "target_cpa": 1500},
                               "after": {"weekly_budget": 26000, "target_cpa": 1800}}


def test_writes_only_named_lever(monkeypatch):
    fake = FakeDirect(monkeypatch, {101: {"strategy": _strategy()}})
    res = bm.apply(_ctx(), [_finding(set_={"weekly_budget": 15000})])
    assert res[1].ok
    holder = fake.camps[101]["strategy"]["Search"]["AverageCpa"]
    assert holder["WeeklySpendLimit"] == 15000 * M and holder["AverageCpa"] == 1500 * M


def test_stale_value_refused(monkeypatch):
    fake = FakeDirect(monkeypatch, {101: {"strategy": _strategy(limit_rub=22000)}})
    res = bm.apply(_ctx(), [_finding()])
    assert not res[1].ok and res[1].error == "changed_in_direct"
    assert fake.updates == []
    assert res[1].response["actual"]["weekly_budget"] == 22000


def test_stale_tolerance_one_ruble(monkeypatch):
    s = _strategy()
    s["Search"]["AverageCpa"]["WeeklySpendLimit"] = 20000 * M + 600_000  # 20 000,60 ₽
    fake = FakeDirect(monkeypatch, {101: {"strategy": s}})
    assert bm.apply(_ctx(), [_finding()])[1].ok
    assert len(fake.updates) == 1


def test_unsupported_campaigns(monkeypatch):
    fake = FakeDirect(monkeypatch, {
        102: {"type": "UNIFIED_CAMPAIGN", "strategy": {}},
        103: {"strategy": _strategy(), "package": 555},
        104: {"strategy": {"Search": {"BiddingStrategyType": "HIGHEST_POSITION"},
                           "Network": {"BiddingStrategyType": "SERVING_OFF"}}},
    })
    items = [_finding(1, cid=101), _finding(2, cid=102), _finding(3, cid=103),
             _finding(4, cid=104, expect={"weekly_budget": None, "target_cpa": None})]
    res = bm.apply(_ctx(), items)
    # 104: рычагов нет, и предела шага без значения «до» не посчитать — действие не той формы
    assert {i: (r.ok, r.error) for i, r in res.items()} == {
        1: (False, "unsupported"), 2: (False, "unsupported"),
        3: (False, "unsupported"), 4: (False, "bad_action")}
    assert fake.updates == []


def test_two_channel_limit_unsupported(monkeypatch):
    s = _strategy()
    s["Network"] = {"BiddingStrategyType": "AVERAGE_CPA",
                    "AverageCpa": {"AverageCpa": 1500 * M, "WeeklySpendLimit": 5000 * M}}
    fake = FakeDirect(monkeypatch, {101: {"strategy": s}})
    res = bm.apply(_ctx(), [_finding()])
    assert (res[1].ok, res[1].error) == (False, "unsupported") and fake.updates == []


def test_direct_accepted_but_not_applied(monkeypatch):
    FakeDirect(monkeypatch, {101: {"strategy": _strategy()}}, accept=False)
    res = bm.apply(_ctx(), [_finding()])
    assert not res[1].ok and "не применил" in res[1].error


def test_bad_action_refused(monkeypatch):
    fake = FakeDirect(monkeypatch, {101: {"strategy": _strategy()}})
    bad = _finding(set_={"weekly_budget": "много"})
    res = bm.apply(_ctx(), [bad])
    assert not res[1].ok and fake.updates == []


def test_rollback_restores_before(monkeypatch):
    fake = FakeDirect(monkeypatch, {101: {"strategy": _strategy(26000, 1800)}})
    f = _finding(status="rollback_requested",
                 response={"before": {"weekly_budget": 20000, "target_cpa": 1500},
                           "after": {"weekly_budget": 26000, "target_cpa": 1800}})
    res = bm.rollback(_ctx(), [f])
    assert res[1].ok, res[1].error
    assert fake.camps[101]["strategy"] == _strategy(20000, 1500)
    assert res[1].response == {"before": {"weekly_budget": 26000, "target_cpa": 1800},
                               "after": {"weekly_budget": 20000, "target_cpa": 1500}}


def test_rollback_stale_refused(monkeypatch):
    fake = FakeDirect(monkeypatch, {101: {"strategy": _strategy(30000, 1800)}})
    f = _finding(status="rollback_requested",
                 response={"before": {"weekly_budget": 20000, "target_cpa": 1500},
                           "after": {"weekly_budget": 26000, "target_cpa": 1800}})
    res = bm.rollback(_ctx(), [f])
    assert not res[1].ok and res[1].error == "changed_in_direct" and fake.updates == []


def test_run_does_not_resolve_approved():
    out = bm.run(_ctx())
    assert out.findings == []
    existing = [_finding(1, status="approved"), _finding(2, cid=102, status="rollback_requested"),
                _finding(3, cid=103, status="applied"),
                _finding(4, cid=104, status="open")]
    for f in existing:
        f.last_seen_at = NOW - timedelta(days=10)
    rec = reconcile("budget", existing, out.findings, NOW, out.scanned_logins,
                    unverified=out.unverified)
    assert rec.resolve == [] and rec.expire == [] and rec.insert == []


def test_never_due():
    assert not runner.is_due("budget", None, None, NOW, NOW)
    assert not runner.is_due("budget", None, None, None, NOW)
    assert runner.is_due("placements", None, None, None, NOW)


# --- раннер: окно отмены и замок -------------------------------------------------------------

class FakeStore:
    """pending_with_now отдаёт очередь и «now() базы» — часы clock, а не процесса."""

    def __init__(self, rows, pending, clock, after_wait=None, outcome_ok=True):
        self.rows, self._pending, self.clock = rows, list(pending), clock
        self.after_wait = after_wait
        self.outcome_ok = outcome_ok
        self.outcomes: List[tuple] = []
        self.runs: Dict[int, dict] = {}
        self.reads = 0

    @contextmanager
    def hold_project_lock(self, slug):
        yield True

    def load_project_modules(self, slug, include_off=False):
        return [r for r in self.rows if include_off or r["mode"] != "off"]

    def fail_stale_runs(self, slug, older_than):
        return 0

    def last_run_started(self, slug, mkey, statuses=None):
        return None

    def project_lead_goals(self, slug):
        return []

    def claim_preview(self, slug, preview_id=None):
        return None

    def cleanup_previews(self, slug, older_than=None):
        return 0

    def cleanup_runs(self, slug, older_than=None):
        return 0

    def start_run(self, slug, mkey, mode, phase=None):
        rid = len(self.runs) + 1
        self.runs[rid] = {"key": mkey, "mode": mode, "phase": phase}
        return rid

    def finish_run(self, rid, status, summary, error=None):
        self.runs[rid].update(status=status, summary=summary, error=error)

    def existing_findings(self, slug, mkey):
        return []

    def pending_with_now(self, slug):
        self.reads += 1
        if self.reads > 1 and self.after_wait is not None:
            return list(self.after_wait), self.clock.t
        return list(self._pending), self.clock.t

    def pending(self, slug):
        return self.pending_with_now(slug)[0]

    def set_outcome(self, fid, status, result, rollback=False):
        self.outcomes.append((fid, status, rollback, result.error))
        return self.outcome_ok


BEFORE = {"weekly_budget": 20000, "target_cpa": 1500}
AFTER = {"weekly_budget": 26000, "target_cpa": 1800}


class SpyModule:
    KEY = "budget"

    def __init__(self):
        self.applied: List[int] = []

    def run(self, ctx):
        return bm.run(ctx)

    def apply(self, ctx, items):
        self.applied += [f.id for f in items]
        return {f.id: ApplyResult(True, {"before": BEFORE, "after": AFTER}) for f in items}

    def rollback(self, ctx, items):
        return {f.id: ApplyResult(True, {}) for f in items}


class Clock:
    """Часы базы; процессные часы тесту не нужны — раннер их для окна не читает."""

    def __init__(self, t, advance=True, on_sleep=None):
        self.t, self.advance, self.slept, self.on_sleep = t, advance, [], on_sleep

    def sleep(self, s):
        self.slept.append(s)
        if self.on_sleep is not None:
            self.on_sleep()
        if self.advance:
            self.t += timedelta(seconds=s)


def _go(store, mod, clock, lock=None, logs=None, cancel=None):
    kw = {"cancel": cancel} if cancel is not None else {}
    return runner.run_project("edunetwork", store=store, accounts=lambda s: (ACC, []),
                              modules={"budget": mod}, now=NOW,
                              log=(logs.append if logs is not None else lambda m: None),
                              agent_lock_fn=lambda s: lock, cabinets=lambda *a: None,
                              sleep=clock.sleep, **kw)


ROW = {"module_key": "budget", "mode": "approve", "settings": {}, "run_requested_at": None}
# Часы базы уходят от метки такта (NOW): окно меряется только ими.
DB_NOW = NOW + timedelta(minutes=5)


def test_skips_recent_decisions():
    # часы не идут: через ожидание находке всё ещё 3 с — в этом проходе не пишется
    clock = Clock(DB_NOW, advance=False)
    young = _finding(1, decided_at=DB_NOW - timedelta(seconds=3))
    old = _finding(2, cid=102, decided_at=DB_NOW - timedelta(seconds=30))
    st, mod = FakeStore([ROW], [young, old], clock), SpyModule()
    _go(st, mod, clock)
    assert mod.applied == [2]
    assert [o[0] for o in st.outcomes] == [2]
    assert clock.slept and 0 < max(clock.slept) <= 20


def test_ripe_threshold_12s_by_db_now():
    # 11 с по часам базы — ещё не зрело (окно Panda 10 с + 2 с запаса), ждём ровно 1 с;
    # по метке такта NOW находка «из будущего» — раннер её не читает
    clock = Clock(DB_NOW, advance=False)
    almost = _finding(1, decided_at=DB_NOW - timedelta(seconds=11))
    ripe = _finding(2, cid=102, decided_at=DB_NOW - timedelta(seconds=12, milliseconds=1))
    st, mod = FakeStore([ROW], [almost, ripe], clock), SpyModule()
    _go(st, mod, clock)
    assert clock.slept == [pytest.approx(1.0)]
    assert mod.applied == [2]


def test_waits_out_undo_window_then_applies():
    clock = Clock(DB_NOW)
    young = _finding(1, decided_at=DB_NOW - timedelta(seconds=3))
    st, mod = FakeStore([ROW], [young], clock), SpyModule()
    _go(st, mod, clock)
    assert clock.slept == [pytest.approx(9.0)]
    assert mod.applied == [1] and st.reads == 2


def test_wait_capped_at_20s():
    clock = Clock(DB_NOW)
    future = _finding(1, decided_at=DB_NOW + timedelta(minutes=3))  # сдвиг часов
    st, mod = FakeStore([ROW], [future], clock), SpyModule()
    _go(st, mod, clock)
    assert clock.slept == [pytest.approx(20.0)] and mod.applied == []


def test_undo_during_wait_writes_nothing():
    clock = Clock(DB_NOW)
    young = _finding(1, decided_at=DB_NOW - timedelta(seconds=3))
    st, mod = FakeStore([ROW], [young], clock, after_wait=[]), SpyModule()
    _go(st, mod, clock)
    assert mod.applied == [] and st.outcomes == []


def test_stop_ends_the_wait():
    switch = runner._cancel.Switch()
    clock = Clock(DB_NOW, on_sleep=switch.request)
    young = _finding(1, decided_at=DB_NOW - timedelta(seconds=3))
    st, mod = FakeStore([ROW], [young], clock), SpyModule()
    assert _go(st, mod, clock, cancel=switch) == runner._cancel.EXIT_CODE
    assert mod.applied == [] and st.outcomes == [] and st.reads == 1


def test_no_wait_for_old_decisions_or_other_modules():
    clock = Clock(DB_NOW)
    st = FakeStore([ROW], [_finding(1, decided_at=DB_NOW - timedelta(minutes=1))], clock)
    mod = SpyModule()
    _go(st, mod, clock)
    assert clock.slept == [] and mod.applied == [1]


def test_outcome_lost_after_write_is_recorded():
    # Директ записан, а находку за это время вернули в open: итог не лёг — след в module_runs
    clock = Clock(DB_NOW)
    logs: List[str] = []
    st = FakeStore([ROW], [_finding(1, decided_at=DB_NOW - timedelta(minutes=1))], clock,
                   outcome_ok=False)
    mod = SpyModule()
    _go(st, mod, clock, logs=logs)
    assert mod.applied == [1]
    run = next(r for r in st.runs.values() if r["phase"] == runner.APPLY_PHASE)
    lost = run["summary"]["apply"]["outcome_lost"]
    assert lost == [{"id": 1, "campaign_id": 101, "rollback": False,
                     "before": BEFORE, "after": AFTER}]
    assert any(m.startswith("ОШИБКА: находка 1") for m in logs)


def test_agent_lock_marks_failed():
    clock = Clock(DB_NOW)
    line = runner.EDU_LOCK_LINE
    st = FakeStore([ROW], [_finding(1, decided_at=DB_NOW - timedelta(minutes=1))], clock)
    mod = SpyModule()
    _go(st, mod, clock, lock=line)
    assert mod.applied == []
    assert st.outcomes == [(1, "failed", False, line)]
    run = next(r for r in st.runs.values() if r["phase"] == runner.APPLY_PHASE)
    assert run["summary"]["agent_lock"] == line


# --- цель CPA как в снимке, отказы движка, восстановление -----------------------------------

def _pay_for_conversion(limit_rub=20000, cpa_rub=1500):
    return {"Search": {"BiddingStrategyType": "PAY_FOR_CONVERSION",
                       "PayForConversion": {"Cpa": cpa_rub * M, "GoalId": 7,
                                            "WeeklySpendLimit": limit_rub * M}},
            "Network": {"BiddingStrategyType": "SERVING_OFF"}}


def _multi_goal(limit_rub=20000):
    return {"Search": {"BiddingStrategyType": "AVERAGE_CPA_MULTIPLE_GOALS",
                       "AverageCpaMultipleGoals": {"WeeklySpendLimit": limit_rub * M}},
            "Network": {"BiddingStrategyType": "SERVING_OFF"}}


GOALS = [{"GoalId": 1, "Value": 900 * M}, {"GoalId": 2, "Value": 1500 * M}]


def test_cpa_read_and_written_in_cpa_field(monkeypatch):
    # PAY_FOR_CONVERSION держит цену в Cpa — снимок читает её (AverageCpa or Cpa), пишем туда же
    fake = FakeDirect(monkeypatch, {101: {"strategy": _pay_for_conversion()}})
    res = bm.apply(_ctx(), [_finding(set_={"target_cpa": 1800})])
    assert res[1].ok, res[1].error
    holder = fake.camps[101]["strategy"]["Search"]["PayForConversion"]
    assert holder["Cpa"] == 1800 * M and "AverageCpa" not in holder
    assert res[1].response["after"] == {"weekly_budget": 20000, "target_cpa": 1800}


def test_multi_goal_cpa_compared_like_snapshot(monkeypatch):
    # у «нескольких целей» снимок берёт цену самой дорогой цели: expect сравнивается с ней
    fake = FakeDirect(monkeypatch, {101: {"strategy": _multi_goal(), "goals": GOALS}})
    res = bm.apply(_ctx(), [_finding(set_={"weekly_budget": 26000})])
    assert res[1].ok, res[1].error
    assert res[1].response["before"] == {"weekly_budget": 20000, "target_cpa": 1500}
    assert len(fake.updates) == 1


def test_multi_goal_cpa_write_unsupported(monkeypatch):
    # цена живёт в PriorityGoals, а не в стратегии — писать в другое поле нельзя
    fake = FakeDirect(monkeypatch, {101: {"strategy": _multi_goal(), "goals": GOALS}})
    res = bm.apply(_ctx(), [_finding(set_={"target_cpa": 1800})])
    assert (res[1].ok, res[1].error) == (False, "unsupported") and fake.updates == []


def test_custom_period_limit_not_written(monkeypatch):
    # снимок читает лимит периода (CustomPeriodBudget), а пишем только WeeklySpendLimit
    s = {"Search": {"BiddingStrategyType": "AVERAGE_CPA",
                    "AverageCpa": {"AverageCpa": 1500 * M,
                                   "CustomPeriodBudget": {"SpendLimit": 20000 * M}}},
         "Network": {"BiddingStrategyType": "SERVING_OFF"}}
    fake = FakeDirect(monkeypatch, {101: {"strategy": s}})
    res = bm.apply(_ctx(), [_finding(set_={"weekly_budget": 26000})])
    assert (res[1].ok, res[1].error) == (False, "unsupported") and fake.updates == []


def test_two_channel_limit_cpa_only_ok(monkeypatch):
    s = _strategy()
    s["Network"] = {"BiddingStrategyType": "AVERAGE_CPA",
                    "AverageCpa": {"AverageCpa": 1500 * M, "WeeklySpendLimit": 5000 * M}}
    fake = FakeDirect(monkeypatch, {101: {"strategy": s}})
    res = bm.apply(_ctx(), [_finding(set_={"target_cpa": 1800})])
    assert res[1].ok, res[1].error
    st = fake.camps[101]["strategy"]
    assert st["Search"]["AverageCpa"]["AverageCpa"] == 1800 * M
    assert st["Network"]["AverageCpa"]["AverageCpa"] == 1500 * M


@pytest.mark.parametrize("expect", [{"weekly_budget": 20000}, {"target_cpa": 1500}, {}])
def test_expect_needs_both_levers(monkeypatch, expect):
    fake = FakeDirect(monkeypatch, {101: {"strategy": _strategy()}})
    res = bm.apply(_ctx(), [_finding(expect=expect, set_={"weekly_budget": 26000})])
    assert (res[1].ok, res[1].error) == (False, "bad_action") and fake.gets == 0


def test_campaign_id_must_match_finding(monkeypatch):
    fake = FakeDirect(monkeypatch, {101: {"strategy": _strategy()}})
    f = _finding()
    f.campaign_id = 999
    res = bm.apply(_ctx(), [f])
    assert (res[1].ok, res[1].error) == (False, "bad_action") and fake.gets == 0


@pytest.mark.parametrize("name", ["EDU | rsv | Поиск", "Наш бренд — Поиск", "КОНКУРЕНТ ВУЗы",
                                  "Бренд RSV"])
def test_protected_campaign_refused(monkeypatch, name):
    fake = FakeDirect(monkeypatch, {101: {"strategy": _strategy(), "name": name}})
    res = bm.apply(_ctx(), [_finding()])
    assert (res[1].ok, res[1].error) == (False, "protected") and fake.updates == []


def test_protected_regex_matches_panda():
    # тот же текст, что EXCLUDED_NAME_SQL в Panda (lib/budget/facts.sql.ts)
    assert bm.EXCLUDED_NAME_RE.pattern == "rsv|Конкурент|Наш бренд"


@pytest.mark.parametrize("set_,ok", [
    ({"weekly_budget": 30000}, True),          # +50 % — ровно предел stepLim
    ({"weekly_budget": 30001}, True),          # допуск 1 ₽ на округление expect
    ({"weekly_budget": 31000}, False),
    ({"weekly_budget": 12000}, True),          # −40 % — предел среза Panda (CUT_STEPS)
    ({"weekly_budget": 11900}, False),
    ({"target_cpa": 1800}, True),              # +20 % — предел stepCpa
    ({"target_cpa": 1850}, False),
    ({"target_cpa": 1200}, True),
    ({"target_cpa": 1150}, False),
])
def test_step_cap(monkeypatch, set_, ok):
    fake = FakeDirect(monkeypatch, {101: {"strategy": _strategy()}})
    res = bm.apply(_ctx(), [_finding(set_=set_)])
    if ok:
        assert res[1].ok, res[1].error
    else:
        assert (res[1].ok, res[1].error) == (False, "bad_action") and fake.gets == 0


def test_step_cap_follows_settings(monkeypatch):
    fake = FakeDirect(monkeypatch, {101: {"strategy": _strategy()}})
    res = bm.apply(_ctx(settings={"stepLim": 20}), [_finding(set_={"weekly_budget": 26000})])
    assert (res[1].ok, res[1].error) == (False, "bad_action") and fake.updates == []


def test_set_without_expect_value_is_bad_action(monkeypatch):
    fake = FakeDirect(monkeypatch, {101: {"strategy": _strategy()}})
    res = bm.apply(_ctx(), [_finding(expect={"weekly_budget": None, "target_cpa": 1500},
                                     set_={"weekly_budget": 20000})])
    assert (res[1].ok, res[1].error) == (False, "bad_action") and fake.gets == 0


def test_non_rub_account_unsupported(monkeypatch):
    fake = FakeDirect(monkeypatch, {101: {"strategy": _strategy()}})
    kzt = [DirectAccount("edu-login", "tok", "KZT", "KZ")]
    res = bm.apply(_ctx(accounts=kzt), [_finding()])
    assert (res[1].ok, res[1].error) == (False, "unsupported") and fake.gets == 0


def test_recovered_when_direct_already_has_set(monkeypatch):
    # прошлый такт записал, но итог не лёг (процесс убит): повторно не пишем, а признаём
    fake = FakeDirect(monkeypatch, {101: {"strategy": _strategy(26000, 1800)}})
    res = bm.apply(_ctx(), [_finding()])
    assert res[1].ok, res[1].error
    assert fake.updates == []
    assert res[1].response == {"before": {"weekly_budget": 20000, "target_cpa": 1500},
                               "after": {"weekly_budget": 26000, "target_cpa": 1800},
                               "recovered": True}


def test_not_recovered_when_other_lever_moved(monkeypatch):
    # set — только лимит, а цену цели после нас правили: это не наш след, а чужая правка
    fake = FakeDirect(monkeypatch, {101: {"strategy": _strategy(26000, 1700)}})
    res = bm.apply(_ctx(), [_finding(set_={"weekly_budget": 26000})])
    assert (res[1].ok, res[1].error) == (False, "changed_in_direct") and fake.updates == []


def test_rollback_recovered_when_already_back(monkeypatch):
    fake = FakeDirect(monkeypatch, {101: {"strategy": _strategy(20000, 1500)}})
    f = _finding(status="rollback_requested",
                 response={"before": {"weekly_budget": 20000, "target_cpa": 1500},
                           "after": {"weekly_budget": 26000, "target_cpa": 1800}})
    res = bm.rollback(_ctx(), [f])
    assert res[1].ok and res[1].response["recovered"] is True and fake.updates == []


def test_unknown_outcome_keeps_before(monkeypatch):
    FakeDirect(monkeypatch, {101: {"strategy": _strategy()}}, write_error=TimeoutError("write"),
               read_error_after_write=TimeoutError("read"))
    res = bm.apply(_ctx(), [_finding()])
    assert not res[1].ok and "TimeoutError" in res[1].error
    assert res[1].response["before"] == {"weekly_budget": 20000, "target_cpa": 1500}


def test_update_warnings_kept(monkeypatch):
    warn = [{"Code": 10000, "Message": "предупреждение"}]
    FakeDirect(monkeypatch, {101: {"strategy": _strategy()}}, warnings=warn)
    res = bm.apply(_ctx(), [_finding()])
    assert res[1].ok and res[1].response["warnings"] == warn


def test_agent_lock_rechecked_after_undo_wait():
    # агент EDU включили, пока раннер ждал окно отмены: писать уже нельзя
    clock = Clock(DB_NOW)
    young = _finding(1, decided_at=DB_NOW - timedelta(seconds=3))
    st, mod = FakeStore([ROW], [young], clock), SpyModule()

    def lock(slug):
        return runner.EDU_LOCK_LINE if clock.slept else None
    runner.run_project("edunetwork", store=st, accounts=lambda s: (ACC, []),
                       modules={"budget": mod}, now=NOW, log=lambda m: None,
                       agent_lock_fn=lock, cabinets=lambda *a: None, sleep=clock.sleep)
    assert clock.slept and mod.applied == []
    assert st.outcomes == [(1, "failed", False, runner.EDU_LOCK_LINE)]


# --- раунд 2: нижние пределы, бюджет обучения, нередактируемые ЕПК ---------------------------

@pytest.mark.parametrize("settings,set_,ok", [
    # срез лимита — не больше 40 % при любом stepLim (limitDown Panda stepLim не читает)
    ({"stepLim": 300}, {"weekly_budget": 11900}, False),
    ({"stepLim": 20}, {"weekly_budget": 12000}, True),
    # подъём цены цели ниже minCpa не ограничен — Panda поднимает и цену 600 ₽
    ({"minCpa": 1000}, {"target_cpa": 1200}, True),
    ({"minCpa": 1300}, {"target_cpa": 1250}, False),
    ({"minCpa": 1300}, {"target_cpa": 1300}, True),
])
def test_lower_bounds(monkeypatch, settings, set_, ok):
    fake = FakeDirect(monkeypatch, {101: {"strategy": _strategy()}})
    res = bm.apply(_ctx(settings=settings), [_finding(set_=set_)])
    if ok:
        assert res[1].ok, res[1].error
    else:
        assert (res[1].ok, res[1].error) == (False, "bad_action") and fake.gets == 0


def test_limit_floor_3000(monkeypatch):
    fake = FakeDirect(monkeypatch, {101: {"strategy": _strategy(limit_rub=4000)}})
    low = _finding(expect={"weekly_budget": 4000, "target_cpa": 1500},
                   set_={"weekly_budget": 2900})
    res = bm.apply(_ctx(), [low])
    assert (res[1].ok, res[1].error) == (False, "bad_action") and fake.gets == 0


def test_cpa_raise_below_min_cpa_allowed(monkeypatch):
    FakeDirect(monkeypatch, {101: {"strategy": _strategy(cpa_rub=600)}})
    res = bm.apply(_ctx(), [_finding(expect={"weekly_budget": 20000, "target_cpa": 600},
                                     set_={"target_cpa": 700})])
    assert res[1].ok, res[1].error


def test_step_settings_clamped():
    s = settings_for("budget", {"stepLim": 1000, "stepCpa": 500, "minCpa": -5})
    assert (s["stepLim"], s["stepCpa"], s["minCpa"]) == (300, 100, 0)
    s = settings_for("budget", {"stepLim": 0, "stepCpa": -3})
    assert (s["stepLim"], s["stepCpa"]) == (1, 1)


def _with_exploration(min_rub, custom="YES"):
    s = _strategy()
    s["Search"]["AverageCpa"]["ExplorationBudget"] = {
        "MinimumExplorationBudget": min_rub * M, "IsMinimumExplorationBudgetCustom": custom}
    return s


def test_lowering_limit_resets_exploration_budget(monkeypatch):
    # ручной бюджет обучения больше нового лимита — Директ отбил бы 4000 (замер 08.09.2026)
    fake = FakeDirect(monkeypatch, {101: {"strategy": _with_exploration(15000)}})
    res = bm.apply(_ctx(), [_finding(set_={"weekly_budget": 12000})])
    assert res[1].ok, res[1].error
    holder = fake.updates[0]["Campaigns"][0]["TextCampaign"]["BiddingStrategy"]["Search"][
        "AverageCpa"]
    assert holder["ExplorationBudget"] == {"MinimumExplorationBudget": 7560 * M,
                                           "IsMinimumExplorationBudgetCustom": "NO"}
    assert holder["WeeklySpendLimit"] == 12000 * M
    assert holder["BidCeiling"] == 300 * M and holder["AverageCpa"] == 1500 * M


def test_exploration_budget_kept_when_fits(monkeypatch):
    fake = FakeDirect(monkeypatch, {101: {"strategy": _with_exploration(5000)}})
    res = bm.apply(_ctx(), [_finding(set_={"weekly_budget": 12000})])
    assert res[1].ok, res[1].error
    holder = fake.updates[0]["Campaigns"][0]["TextCampaign"]["BiddingStrategy"]["Search"][
        "AverageCpa"]
    assert holder["ExplorationBudget"] == {"MinimumExplorationBudget": 5000 * M,
                                           "IsMinimumExplorationBudgetCustom": "YES"}


STUB = "Невалидное значение: CampaignStub{type=UNIFIED_CAMPAIGN, id=709413835}"


def test_campaign_stub_item_error_not_editable(monkeypatch):
    fake = FakeDirect(monkeypatch, {101: {"strategy": _strategy()}}, accept=False)
    fake.item_errors = [{"Code": 5005, "Message": "Неверное значение", "Details": STUB}]
    res = bm.apply(_ctx(), [_finding()])
    assert (res[1].ok, res[1].error) == (False, "not_editable")
    assert res[1].response["before"] == {"weekly_budget": 20000, "target_cpa": 1500}
    assert len(fake.updates) == 1


def test_campaign_stub_request_error_not_editable(monkeypatch):
    err = bm.direct.DirectError("campaigns [5005] Неверное значение: " + STUB, code=5005)
    FakeDirect(monkeypatch, {101: {"strategy": _strategy()}}, accept=False, write_error=err)
    res = bm.apply(_ctx(), [_finding()])
    assert (res[1].ok, res[1].error) == (False, "not_editable")


def test_other_5005_is_plain_error(monkeypatch):
    # 5005 без CampaignStub — ошибка нашего тела, а не «правка только руками»
    fake = FakeDirect(monkeypatch, {101: {"strategy": _strategy()}}, accept=False)
    fake.item_errors = [{"Code": 5005, "Message": "Неверное значение", "Details": "BidCeiling"}]
    res = bm.apply(_ctx(), [_finding()])
    assert not res[1].ok and res[1].error.startswith("DirectError") and "5005" in res[1].error


def test_direct_error_carries_code(monkeypatch):
    class Resp:
        status = 200

        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    body = '{"error": {"error_code": 5005, "error_string": "x", "error_detail": "y"}}'
    monkeypatch.setattr(bm.direct.urllib.request, "urlopen",
                        lambda req, timeout=None: Resp(body.encode("utf-8")))
    with pytest.raises(bm.direct.DirectError) as e:
        bm.direct.call("t", "l", "campaigns", {})
    assert e.value.code == 5005
