# -*- coding: utf-8 -*-
"""Ротация своих запретов в полном списке кампании и кабинет проекта synergy."""

from datetime import timedelta

import pytest

from sync.modules import cabinets, runner
from sync.modules import placements as pm
from sync.modules.accounts import (SYNERGY_SCHOOL_LOGIN, DirectAccount,
                                   accounts_for)
from sync.modules.lifecycle import reconcile, rotated
from sync.modules.types import ApplyResult, StoredFinding, fingerprint
from sync.placements import direct
from sync.placements import plan as planner
from tests.test_modules_placements import (ACC, NOW, FakeDirect, _camp, _ctx, _stored)

CEIL = planner.FILL_CEILING  # 900 при лимите 1000
JUNK = ["com.new.one", "com.new.two", "com.new.three"]


def _human(n, prefix="manual"):
    return ["%s%d.ru" % (prefix, i) for i in range(n)]


def _own(i, site, cid=1, rule="app", age_days=0, status="applied"):
    return StoredFinding(
        id=i, status=status, fingerprint="placements:%s:login1:%d:%s" % (rule, cid, site),
        last_seen_at=NOW, rule=rule, severity="info", title="t", login="login1",
        campaign_id=cid, campaign_name="К", object_type="site", object_id=site,
        details={}, action={"op": "exclude_site", "campaign_id": cid, "site": site},
        applied_at=NOW - timedelta(days=age_days))


def _rows(cid, sites, clicks=50):
    return [{"campaign_id": str(cid), "placement": s, "clicks": clicks - i, "cost": 9.0}
            for i, s in enumerate(sites)]


# --- план: ёмкость ротацией ---------------------------------------------------------------

def test_plan_full_list_without_own_still_refuses():
    camp = _camp(1, items=_human(CEIL))
    plan = planner.plan_account(_rows(1, JUNK), [camp])
    assert plan["actions"] == []
    assert "список запретов заполнен" in plan["refused"][0]["reason"]


def test_plan_full_list_rotates_oldest_own_exactly_as_needed():
    own = ["com.old.a", "com.old.b", "com.old.c", "com.old.d"]
    camp = _camp(1, items=_human(CEIL - len(own)) + own)
    plan = planner.plan_account(_rows(1, JUNK), [camp], own={"1": own})
    (action,) = plan["actions"]
    assert [a["placement"] for a in action["added"]] == JUNK
    assert action["evict"] == own[:3]
    assert len(action["sites"]) == CEIL
    assert "com.old.d" in action["sites"] and "com.old.a" not in action["sites"]
    assert set(_human(CEIL - len(own))) <= set(action["sites"])


def test_plan_list_over_ceiling_by_human_swaps_one_to_one():
    """Человек сам заполнил выше потолка (990, из них своих 100): одна новая снимает
    одну свою, список не растёт и не опускается."""
    own = ["com.old.%d" % i for i in range(100)]
    camp = _camp(1, items=_human(990 - 100) + own)
    plan = planner.plan_account(_rows(1, JUNK[:1]), [camp], own={"1": own})
    (action,) = plan["actions"]
    assert action["evict"] == own[:1]
    assert action["fill_after"] == 990


def test_plan_list_at_limit_never_exceeds_limit():
    own = ["com.old.%d" % i for i in range(2)]
    camp = _camp(1, items=_human(1000 - 2) + own)
    plan = planner.plan_account(_rows(1, JUNK), [camp], own={"1": own})
    (action,) = plan["actions"]
    assert action["evict"] == own and len(action["added"]) == 2
    assert action["fill_after"] == 1000


def test_rotation_need_bounds():
    # под потолком: сначала свободное
    assert planner.rotation_need(3, 898, 900, 1000) == 1
    # выше потолка (человек): 1:1, не больше числа новых
    assert planner.rotation_need(1, 990, 900, 1000) == 1
    assert planner.rotation_need(2, 908, 900, 1000) == 2
    # на лимите
    assert planner.rotation_need(1, 1000, 900, 1000) == 1
    assert planner.free_slots(990, 900, 1000) == 0 and planner.free_slots(10, 900, 1000) == 890


def test_plan_capacity_limited_by_own_count():
    own = ["com.old.a"]
    camp = _camp(1, items=_human(CEIL - 1) + own)
    plan = planner.plan_account(_rows(1, JUNK), [camp], own={"1": own})
    (action,) = plan["actions"]
    assert len(action["added"]) == 1 and action["evict"] == own
    assert any("отложено 2" in r["reason"] for r in plan["refused"])


def test_plan_never_evicts_human_variant_or_pinned():
    """Свой по журналу, но в списке в другом регистре — ставил человек; пришпиленное
    «Всегда закрывать» не снимается, даже если робот писал его сам."""
    own = ["com.old.a", "forced.ru"]
    camp = _camp(1, items=_human(CEIL - 2) + ["COM.OLD.A", "forced.ru"])
    plan = planner.plan_account(_rows(1, JUNK), [camp], own={"1": own},
                                always_block=["forced.ru"])
    assert plan["actions"] == []
    assert "заполнен" in plan["refused"][0]["reason"]


def test_evictable_order_and_filters():
    items = ["a.ru", "B.ru", "c.ru ", "d.ru"]
    assert planner.evictable(["d.ru", "b.ru", "a.ru", "c.ru", "zz.ru"], items, keep=["a.ru"]) \
        == ["d.ru", "c.ru"]


# --- скан модуля --------------------------------------------------------------------------

@pytest.fixture
def full(monkeypatch):
    own = ["com.old.%d" % i for i in range(5)]
    state = {"campaigns": [_camp(1, items=_human(CEIL - 5) + own)],
             "rows": _rows(1, JUNK), "own": own}
    monkeypatch.setattr(direct, "campaigns", lambda t, l, ids=(): state["campaigns"])
    monkeypatch.setattr(direct, "placements_today", lambda t, l: state["rows"])
    monkeypatch.setattr(direct, "placements_window", lambda *a: [])
    return state


def _ctx_with(applied, settings=None):
    ctx = _ctx(settings)
    ctx.applied = lambda login: applied if login == "login1" else []
    return ctx


def test_scan_finds_when_room_comes_from_rotation(full):
    # возраст: com.old.4 — старейший, com.old.0 — свежий
    applied = [_own(10 + i, s, age_days=i) for i, s in enumerate(full["own"])]
    out = pm.run(_ctx_with(applied))
    assert sorted(f.object_id for f in out.findings) == sorted(JUNK)
    assert out.findings[0].details["rotation"] == {
        "evict": ["com.old.4", "com.old.3", "com.old.2"]}
    assert not [r for r in out.summary["refused"] if "заполнен" in str(r)]


def test_scan_without_own_keeps_old_refusal(full):
    out = pm.run(_ctx_with([]))
    assert out.findings == []
    assert any("заполнен" in str(r) for r in out.summary["refused"])


def test_scan_own_history_failure_means_no_rotation(full):
    ctx = _ctx()

    def boom(login):
        raise RuntimeError("база лежит")
    ctx.applied = boom
    out = pm.run(ctx)
    assert out.findings == [] and out.scanned_logins == ["login1"]


def test_own_by_campaign_skips_forced_spam_and_always_block():
    applied = [_own(1, "a.ru", age_days=1), _own(2, "b.ru", rule="spam", age_days=9),
               _own(3, "c.ru", rule="forced", age_days=9), _own(4, "d.ru", age_days=5),
               _own(5, "e.ru", age_days=7), _own(6, "f.ru", cid=2)]
    got = pm.own_by_campaign(applied, always_block=["E.ru"])
    assert [f.object_id for f in got[1]] == ["d.ru", "a.ru"]
    assert [f.object_id for f in got[2]] == ["f.ru"]


# --- применение ---------------------------------------------------------------------------

def test_apply_rotates_only_own_oldest_first(monkeypatch):
    own_sites = ["com.old.a", "com.old.b", "com.old.c"]
    manual = _human(CEIL - 3)
    fake = FakeDirect(monkeypatch, [_camp(1, items=manual + own_sites)])
    own = [_own(21, "com.old.a", age_days=1), _own(22, "com.old.b", age_days=30),
           _own(23, "com.old.c", age_days=10)]
    ctx = _ctx()
    ctx.applied = lambda login: own
    res = pm.apply(ctx, [_stored(1, JUNK[0]), _stored(2, JUNK[1])])
    assert res[1].ok and res[2].ok
    (cid, written), = fake.writes
    assert written == manual + ["com.old.a"] + JUNK[:2]
    assert len(written) == CEIL
    assert [d["id"] for d in res[1].displaced] == [22]
    assert [d["id"] for d in res[2].displaced] == [23]
    assert res[1].displaced[0] == {"id": 22, "site": "com.old.b", "for_site": JUNK[0],
                                   "campaign_id": 1}
    assert res[2].response["rotated_out"] == ["com.old.c"]


def test_apply_without_own_still_refuses_full(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(1, items=_human(CEIL))])
    res = pm.apply(_ctx(), [_stored(1, JUNK[0])])
    assert not res[1].ok and "заполнен" in res[1].error
    assert fake.writes == []


def test_apply_never_touches_manual_even_if_named_like_own(monkeypatch):
    """Журнал говорит «свой», но в списке форма человека (регистр) — не снимаем."""
    fake = FakeDirect(monkeypatch, [_camp(1, items=_human(CEIL - 1) + ["COM.OLD.A"])])
    ctx = _ctx()
    ctx.applied = lambda login: [_own(21, "com.old.a")]
    res = pm.apply(ctx, [_stored(1, JUNK[0])])
    assert not res[1].ok and fake.writes == []


def test_apply_over_ceiling_by_human_swaps_one_to_one(monkeypatch):
    own_sites = ["com.old.%d" % i for i in range(100)]
    fake = FakeDirect(monkeypatch, [_camp(1, items=_human(990 - 100) + own_sites)])
    ctx = _ctx()
    ctx.applied = lambda login: [_own(100 + i, s, age_days=200 - i)
                                 for i, s in enumerate(own_sites)]
    res = pm.apply(ctx, [_stored(1, JUNK[0]), _stored(2, JUNK[1])])
    assert res[1].ok and res[2].ok
    assert len(fake.writes[0][1]) == 990
    assert [d["site"] for d in res[1].displaced] == own_sites[:1]
    assert [d["site"] for d in res[2].displaced] == own_sites[1:2]


def test_apply_at_limit_never_exceeds_limit(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(1, items=_human(999) + ["com.old.a"])])
    ctx = _ctx()
    ctx.applied = lambda login: [_own(21, "com.old.a")]
    res = pm.apply(ctx, [_stored(1, JUNK[0]), _stored(2, JUNK[1])])
    assert res[1].ok and not res[2].ok and "заполнен" in res[2].error
    assert len(fake.writes[0][1]) == 1000


def test_apply_respects_hold_from_scan(monkeypatch):
    """Спам-пин этого скана (hold) применение не снимает, как и скан."""
    fake = FakeDirect(monkeypatch, [_camp(1, items=_human(CEIL - 2) + ["spam.ru", "com.old.a"])])
    ctx = _ctx()
    ctx.applied = lambda login: [_own(21, "spam.ru", age_days=9), _own(22, "com.old.a")]
    f = _stored(1, JUNK[0])
    f.details = {"rotation": {"evict": ["com.old.a"], "hold": ["spam.ru"]}}
    res = pm.apply(ctx, [f])
    assert res[1].ok and [d["site"] for d in res[1].displaced] == ["com.old.a"]
    assert "spam.ru" in fake.c[1]["ExcludedSites"]["Items"]


def test_apply_displaced_only_what_left_the_list(monkeypatch):
    """Директ не снял вытесняемую — её не помечаем снятой."""
    class Sticky(FakeDirect):
        def update(self, token, login, cid, field, items):
            self.writes.append((cid, list(items)))
            self.c[cid]["ExcludedSites"] = {"Items": list(items) + ["com.old.a"]}
            return {"Id": cid}

    Sticky(monkeypatch, [_camp(1, items=_human(CEIL - 1) + ["com.old.a"])])
    ctx = _ctx()
    ctx.applied = lambda login: [_own(21, "com.old.a")]
    res = pm.apply(ctx, [_stored(1, JUNK[0])])
    assert res[1].ok and res[1].displaced == []


def test_approved_finding_in_approve_mode_rotates_too(monkeypatch):
    """Режим «одобрять»: одобренная человеком находка при применении ротирует так же."""
    fake = FakeDirect(monkeypatch, [_camp(1, items=_human(CEIL - 1) + ["com.old.a"])])
    ctx = _ctx()
    ctx.mode = "approve"
    ctx.applied = lambda login: [_own(21, "com.old.a")]
    res = pm.apply(ctx, [_stored(1, JUNK[0])])
    assert res[1].ok and [d["id"] for d in res[1].displaced] == [21]
    assert "com.old.a" not in fake.c[1]["ExcludedSites"]["Items"]


# --- раннер и жизненный цикл ----------------------------------------------------------------

class _DispStore:
    def __init__(self, ok=True):
        self.marked, self.ok = [], ok

    def mark_displaced(self, fid, note):
        self.marked.append((fid, note))
        return self.ok


def test_runner_records_displaced_as_rotated():
    st, out = _DispStore(), {}
    results = {1: ApplyResult(True, {}, displaced=[
        {"id": 22, "site": "com.old.b", "for_site": "com.new.one", "campaign_id": 1}]),
        2: ApplyResult(False, {}, "x")}
    runner._record_displaced(st, results, out, lambda m: None)
    assert out == {"rotated_out": 1}
    fid, note = st.marked[0]
    assert fid == 22 and note["for_finding_id"] == 1 and note["for_site"] == "com.new.one"
    assert "снят ротацией" in note["reason"]


def test_runner_applied_reader_reads_once_per_login():
    calls = []

    class S:
        def applied_findings(self, slug, mkey, login):
            calls.append(login)
            return [login]

    ctx = runner._ctx("edunetwork", "placements", "auto", {}, [ACC], NOW, lambda m: None,
                      store=S())
    assert ctx.applied("a") == ["a"] and ctx.applied("a") == ["a"] and ctx.applied("b") == ["b"]
    assert calls == ["a", "b"]


def _rb(status, response):
    f = _own(5, "com.old.a", status=status)
    f.response = response
    return f


def test_rotated_out_site_found_again_is_inserted():
    """Снятая ротацией площадка вернулась — снова находка (старая не глушит)."""
    old = _rb("rolled_back", {"rotated": {"for_site": "x"}})
    old.outcome_at = NOW
    assert rotated(old)
    new = pm.Finding(rule="app", severity="info", title="t", login="login1", campaign_id=1,
                     campaign_name="К", object_type="site", object_id="com.old.a", details={},
                     action={"op": "exclude_site", "campaign_id": 1, "site": "com.old.a"})
    assert fingerprint("placements", new) == old.fingerprint
    rec = reconcile("placements", [old], [new], NOW)
    assert rec.insert == [new]


def test_human_rollback_still_suppresses():
    old = _rb("rolled_back", {"rollback": {"ok": True}})
    old.outcome_at = NOW
    assert not rotated(old)
    new = pm.Finding(rule="app", severity="info", title="t", login="login1", campaign_id=1,
                     campaign_name="К", object_type="site", object_id="com.old.a", details={},
                     action={"op": "exclude_site", "campaign_id": 1, "site": "com.old.a"})
    assert reconcile("placements", [old], [new], NOW).insert == []


# --- synergy: весь кабинет проекта, кампании — галочками настроек ---------------------

def test_accounts_for_synergy_school_is_whole_cabinet():
    (acc,), why = accounts_for("synergy", {"APPROVE_AGENCY_TOKEN": "t"})
    assert why == []
    assert (acc.login, acc.token, acc.currency) == (SYNERGY_SCHOOL_LOGIN, "t", "RUB")
    assert acc.campaign_ids is None
    got, _ = cabinets.select_accounts([acc], [], {SYNERGY_SCHOOL_LOGIN: [715539609, 7]})
    assert got[0].campaign_ids == (7, 715539609)
    acc2, why = accounts_for("synergy", {})
    assert acc2 == [] and "APPROVE_AGENCY_TOKEN" in why[0]


def test_inventory_reads_whole_synergy_cabinet():
    params = []

    def call_fn(token, login, service, payload):
        params.append(payload["params"]["SelectionCriteria"])
        return {"Campaigns": [{"Id": 1, "Name": "a", "Type": "TEXT_CAMPAIGN", "State": "ON"},
                              {"Id": 7, "Name": "новая", "Type": "TEXT_CAMPAIGN",
                               "State": "ON"}]}
    acc = DirectAccount(SYNERGY_SCHOOL_LOGIN, "t", "RUB", "ОШ")
    got = cabinets.read_campaigns(acc, call_fn)
    assert "Ids" not in params[0]
    assert [c["id"] for c in got] == [1, 7]


SYN = DirectAccount(SYNERGY_SCHOOL_LOGIN, "t", "RUB", "ОШ", campaign_ids=(1, 2))


def test_scan_of_selected_campaigns_reads_only_them(monkeypatch):
    asked = {}

    def campaigns(t, l, ids=()):
        asked["ids"] = tuple(ids)
        return [_camp(1), _camp(2)]

    rows = _rows(1, JUNK[:1]) + _rows(7, JUNK[1:2])
    monkeypatch.setattr(direct, "campaigns", campaigns)
    monkeypatch.setattr(direct, "placements_today", lambda t, l: rows)
    monkeypatch.setattr(direct, "placements_window", lambda *a: [])
    out = pm.run(_ctx(accounts=(SYN,)))
    assert asked["ids"] == (1, 2)
    assert [(f.campaign_id, f.object_id) for f in out.findings] == [(1, JUNK[0])]


# --- скан: hold, самовосстановление, перенесённая история ---------------------------------

def test_scan_hold_matches_apply_for_spam_pin(monkeypatch, full):
    """Своя площадка, которую этот прогон пришпилил спамом, в evict не попадает и едет в
    hold — применение увидит тот же набор."""
    spam_site = full["own"][4]  # старейшая из своих
    monkeypatch.setattr(pm._Login, "_spam", lambda self: ({spam_site: "спам"}, None))
    applied = [_own(10 + i, s, age_days=i) for i, s in enumerate(full["own"])]
    out = pm.run(_ctx_with(applied, {"spam_rule": True}))
    rot = out.findings[0].details["rotation"]
    assert rot["hold"] == [spam_site]
    assert spam_site not in rot["evict"] and len(rot["evict"]) == 3


def test_scan_closes_own_applied_absent_from_campaign(full):
    """Запись ротации прошла, чтение обратно упало: в журнале applied, в Директе запрета
    нет. Скан отдаёт такую находку раннеру на закрытие и не считает её своей для ротации."""
    applied = [_own(10 + i, s, age_days=i) for i, s in enumerate(full["own"])]
    lost = _own(99, "com.lost.one", age_days=100)
    variant = _own(98, "com.case.one", age_days=100)
    full["campaigns"][0]["ExcludedSites"]["Items"].append("COM.CASE.ONE")
    other = _own(97, "com.other.one", cid=5, age_days=100)  # кампанию не читали
    out = pm.run(_ctx_with(applied + [lost, variant, other]))
    assert [d["id"] for d in out.displaced] == [98, 99]  # равный возраст — по id
    assert out.displaced[1] == {"id": 99, "site": "com.lost.one", "campaign_id": 1,
                                "absent": True, "reason": pm.ABSENT_REASON}
    assert "com.lost.one" not in out.findings[0].details["rotation"]["evict"]


def test_migrated_history_counts_as_own():
    """Перенос synergy_school -> synergy: fingerprint placements:verdict:login:cid:site,
    status applied, details.migrated_from; с action и без него."""
    with_action = _own(1, "com.mig.a", age_days=40)
    with_action.details = {"migrated_from": "placement_cleaner_cuts"}
    bare = _own(2, "com.mig.b", age_days=30)
    bare.action = None
    bare.details = {"migrated_from": "placement_cleaner_cuts"}
    other = _own(3, "x", age_days=1)
    other.action = {"op": "minus_word", "word": "x"}
    got = pm.own_by_campaign([with_action, bare, other])
    assert [f.id for f in got[1]] == [1, 2]
    assert [d["id"] for d in pm.absent_own([with_action, bare], [_camp(1, items=["com.mig.a"])])] \
        == [2]


def test_runner_records_absent_before_reconcile(monkeypatch):
    from tests.test_modules_runner import FakeModule, FakeStore, _go, _row

    order = []

    class St(FakeStore):
        def mark_displaced(self, fid, note):
            order.append(("mark", fid, note["absent"], note["reason"]))
            return True

        def existing_findings(self, slug, mkey):
            order.append(("existing",))
            return super().existing_findings(slug, mkey)

    class M(FakeModule):
        def run(self, ctx):
            out = super().run(ctx)
            out.displaced = [{"id": 7, "site": "a.ru", "campaign_id": 1, "absent": True,
                              "reason": pm.ABSENT_REASON}]
            return out

    st = St([_row(mode="report")])
    assert _go(st, {"placements": M("placements")}) == 0
    assert order[0] == ("mark", 7, True, pm.ABSENT_REASON)
    assert order[1] == ("existing",)
    run = next(r for r in st.runs.values() if r.get("phase") is None)
    assert run["summary"]["absent_closed"] == 1
