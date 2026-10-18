# -*- coding: utf-8 -*-
from datetime import datetime, timedelta, timezone

from sync.modules.lifecycle import (EXPIRE_HOURS, FAILED_RETRY_HOURS, REJECT_SUPPRESS_DAYS, can_transition,
                                    reconcile)
from sync.modules.types import Finding, StoredFinding, fingerprint

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
MK = "placements"


def finding(obj="site.ru", action=None, **kw):
    base = dict(rule="junk", severity="info", title="t", login="acc", campaign_id=7,
                campaign_name="К", object_type="site", object_id=obj, details={"clicks": 1},
                action=action)
    base.update(kw)
    return Finding(**base)


def stored(f, id=1, status="open", seen_ago=timedelta(hours=1), decided_ago=None,
           outcome_ago=None):
    return StoredFinding(**{**f.__dict__}, id=id, status=status,
                         fingerprint=fingerprint(MK, f), last_seen_at=NOW - seen_ago,
                         decided_at=None if decided_ago is None else NOW - decided_ago,
                         outcome_at=None if outcome_ago is None else NOW - outcome_ago)


ACT = {"op": "exclude_site"}


def test_fingerprint_format():
    assert fingerprint(MK, finding()) == "placements:junk:acc:7:site.ru"
    assert fingerprint(MK, finding(campaign_id=None)) == "placements:junk:acc::site.ru"


def test_new_finding_is_inserted():
    f = finding()
    r = reconcile(MK, [], [f], NOW)
    assert r.insert == [f] and not r.touch and not r.resolve and not r.expire


def test_duplicate_seen_inserted_once():
    f = finding()
    assert reconcile(MK, [], [f, finding()], NOW).insert == [f]


def test_open_seen_again_is_touched_not_inserted():
    f = finding(action=ACT)
    r = reconcile(MK, [stored(f, id=5)], [f], NOW)
    assert r.insert == [] and r.touch == [(5, f)] and r.expire == []


def test_open_audit_not_seen_is_resolved():
    r = reconcile(MK, [stored(finding(), id=3)], [], NOW)
    assert r.resolve == [3] and r.expire == []


def test_open_action_not_seen_73h_expires_but_71h_does_not():
    f = finding(action=ACT)
    assert EXPIRE_HOURS == 72
    r = reconcile(MK, [stored(f, id=1, seen_ago=timedelta(hours=73))], [], NOW)
    assert r.expire == [1] and r.resolve == []
    r = reconcile(MK, [stored(f, id=1, seen_ago=timedelta(hours=71))], [], NOW)
    assert r.expire == [] and r.resolve == []


def test_non_open_not_seen_is_left_alone():
    f = finding(action=ACT)
    for st in ("approved", "applied", "rollback_requested"):
        r = reconcile(MK, [stored(f, status=st, seen_ago=timedelta(days=9))], [], NOW)
        assert not r.resolve and not r.expire


def test_rejected_suppresses_30_days_then_reinserts():
    f = finding(action=ACT)
    assert REJECT_SUPPRESS_DAYS == 30
    r = reconcile(MK, [stored(f, status="rejected", decided_ago=timedelta(days=10))], [f], NOW)
    assert r.insert == []
    r = reconcile(MK, [stored(f, status="rejected", decided_ago=timedelta(days=31))], [f], NOW)
    assert r.insert == [f]


def test_dismissed_never_reinserted():
    f = finding()
    r = reconcile(MK, [stored(f, status="dismissed", decided_ago=timedelta(days=400))], [f], NOW)
    assert r.insert == [] and r.touch == []


def test_live_statuses_block_insert():
    f = finding(action=ACT)
    for st in ("applied", "approved", "rollback_requested"):
        r = reconcile(MK, [stored(f, status=st)], [f], NOW)
        assert r.insert == [], st


def test_old_rejected_plus_live_open_touches():
    f = finding(action=ACT)
    ex = [stored(f, id=1, status="rejected", decided_ago=timedelta(days=40)),
          stored(f, id=2, status="open")]
    r = reconcile(MK, ex, [f], NOW)
    assert r.insert == [] and r.touch == [(2, f)]


def test_can_transition():
    yes = [("open", "approved"), ("open", "rejected"), ("open", "dismissed"),
           ("approved", "applied"), ("approved", "failed"), ("applied", "rollback_requested"),
           ("rollback_requested", "rolled_back"), ("open", "resolved"), ("open", "expired")]
    no = [("rejected", "approved"), ("applied", "approved"), ("dismissed", "open"),
          ("open", "applied")]
    assert all(can_transition(a, b) for a, b in yes)
    assert not any(can_transition(a, b) for a, b in no)


def test_transitions_for_refused_rollback_and_failed():
    assert can_transition("rollback_requested", "applied")
    assert not can_transition("rollback_requested", "failed")
    assert can_transition("approved", "failed")
    assert not can_transition("applied", "failed")


def test_failed_login_open_finding_is_not_resolved_or_expired():
    audit = finding(login="bad")
    act = finding(obj="x.ru", login="bad", action=ACT)
    old = timedelta(hours=100)
    ex = [stored(audit, id=1, seen_ago=old), stored(act, id=2, seen_ago=old)]
    r = reconcile(MK, ex, [], NOW, scanned_logins=["good"])
    assert r.resolve == [] and r.expire == []
    r = reconcile(MK, ex, [], NOW, scanned_logins=["bad"])
    assert r.resolve == [1] and r.expire == [2]
    r = reconcile(MK, ex, [], NOW)
    assert r.resolve == [1] and r.expire == [2]


def test_live_non_open_seen_again_goes_to_seen_live_only():
    f = finding(action=ACT)
    for st in ("approved", "applied", "rollback_requested"):
        r = reconcile(MK, [stored(f, id=9, status=st)], [f], NOW)
        assert r.seen_live == [9] and r.touch == [] and r.insert == [], st


def test_rejected_without_decided_at_uses_last_seen_at():
    f = finding(action=ACT)
    fresh = stored(f, status="rejected", seen_ago=timedelta(days=2))
    old = stored(f, status="rejected", seen_ago=timedelta(days=45))
    assert reconcile(MK, [fresh], [f], NOW).insert == []
    assert reconcile(MK, [old], [f], NOW).insert == [f]


def test_rolled_back_suppresses_30_days():
    f = finding(action=ACT)
    r = reconcile(MK, [stored(f, status="rolled_back", decided_ago=timedelta(days=5))], [f], NOW)
    assert r.insert == []
    r = reconcile(MK, [stored(f, status="rolled_back", decided_ago=timedelta(days=31))], [f], NOW)
    assert r.insert == [f]


def test_failed_suppresses_24_hours():
    f = finding(action=ACT)
    assert FAILED_RETRY_HOURS == 24
    r = reconcile(MK, [stored(f, status="failed", decided_ago=timedelta(hours=23))], [f], NOW)
    assert r.insert == []
    r = reconcile(MK, [stored(f, status="failed", decided_ago=timedelta(hours=25))], [f], NOW)
    assert r.insert == [f]


def test_outcome_windows_measured_from_outcome_at():
    f = finding(action=ACT)
    # одобрено давно, упало только что: окно считается от итога
    r = reconcile(MK, [stored(f, status="failed", decided_ago=timedelta(days=5),
                              outcome_ago=timedelta(hours=1))], [f], NOW)
    assert r.insert == []
    r = reconcile(MK, [stored(f, status="rolled_back", decided_ago=timedelta(days=1),
                              outcome_ago=timedelta(days=40))], [f], NOW)
    assert r.insert == [f]


def test_project_level_login_counts_as_scanned():
    proj = finding(login="")
    ex = [stored(proj, id=1)]
    assert reconcile(MK, ex, [], NOW, scanned_logins=["good"]).resolve == [1]
    assert reconcile(MK, ex, [], NOW, scanned_logins=[]).resolve == []
    assert reconcile(MK, ex, [], NOW).resolve == [1]


def test_suppression_by_action_across_rules():
    old = finding(action=ACT, rule="word_zero_conv")
    new = finding(action=ACT, rule="stop_word")
    # (a) отказ человека держит то же действие под другим правилом
    ex = [stored(old, status="rejected", decided_ago=timedelta(days=1))]
    assert reconcile(MK, ex, [new], NOW).insert == []
    # (b) откат: 30 дней держит, 31 — нет
    ex = [stored(old, status="rolled_back", outcome_ago=timedelta(days=29))]
    assert reconcile(MK, ex, [new], NOW).insert == []
    ex = [stored(old, status="rolled_back", outcome_ago=timedelta(days=31))]
    assert reconcile(MK, ex, [new], NOW).insert == [new]
    # dismissed — навсегда, failed — 24 часа
    ex = [stored(old, status="dismissed", seen_ago=timedelta(days=90))]
    assert reconcile(MK, ex, [new], NOW).insert == []
    ex = [stored(old, status="failed", outcome_ago=timedelta(hours=30))]
    assert reconcile(MK, ex, [new], NOW).insert == [new]


def test_suppression_by_action_needs_same_identity():
    old = stored(finding(action=ACT, rule="a"), status="rejected", decided_ago=timedelta(days=1))
    # (c) другой op или другая кампания — не то же действие
    other_op = finding(action={"op": "minus_phrase"}, rule="b")
    other_camp = finding(action=ACT, rule="b", campaign_id=8)
    assert reconcile(MK, [old], [other_op], NOW).insert == [other_op]
    assert reconcile(MK, [old], [other_camp], NOW).insert == [other_camp]


def test_findings_without_action_suppressed_by_fingerprint_only():
    # (d) аудит: отказ по одному правилу не гасит другое правило того же объекта
    old = stored(finding(rule="a"), status="rejected", decided_ago=timedelta(days=1))
    other = finding(rule="b")
    assert reconcile(MK, [old], [other], NOW).insert == [other]
    assert reconcile(MK, [old], [finding(rule="a")], NOW).insert == []
