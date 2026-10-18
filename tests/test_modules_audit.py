# -*- coding: utf-8 -*-
"""Сбор снимка audit и run(): формы запросов, отказ кабинета, режимы."""

from datetime import datetime, timezone

import pytest

from sync.modules import audit as audit_mod
from sync.modules.accounts import DirectAccount
from sync.modules.defaults import ALLOWED_MODES, settings_for
from sync.modules.types import Context

NOW = datetime(2026, 10, 18, 22, 30, tzinfo=timezone.utc)  # 19.10 по Москве
ACC = DirectAccount("login1", "tok", "RUB", "X")


def _ctx(accounts=(ACC,), settings=None):
    return Context(slug="edunetwork", module_key="audit", mode="report",
                   settings=settings_for("audit", settings), accounts=list(accounts),
                   now=NOW, log=lambda m: None)


class Fake:
    def __init__(self, monkeypatch, n_campaigns=12, fail_login=None):
        self.calls, self.reports = [], []
        self.n = n_campaigns
        self.fail_login = fail_login
        monkeypatch.setattr(audit_mod.direct_api, "get_all", self.get_all)
        monkeypatch.setattr(audit_mod.direct_api, "report_rows", self.report_rows)

    def get_all(self, token, login, service, criteria, fields, extra=None, **kw):
        self.calls.append((login, service, criteria, fields, extra, kw))
        if login == self.fail_login and service == "ads":
            raise RuntimeError("Директ лёг")
        if service == "campaigns":
            rows = [{"Id": i, "Name": "К%d" % i, "Type": "TEXT_CAMPAIGN", "State": "ON",
                     "TextCampaign": {"CounterIds": [1],
                                      "BiddingStrategy": {"Search": {}}}}
                    for i in range(1, self.n + 1)]
            return rows + [{"Id": 999, "Type": "TEXT_CAMPAIGN", "State": "SUSPENDED"}]
        if service == "audiencetargets":
            return [{"AdGroupId": 10, "State": "ON"}, {"AdGroupId": 10, "State": "SUSPENDED"}]
        return []

    def report_rows(self, token, login, params, *, prefix, **kw):
        self.reports.append(params)
        return [{"CampaignId": "1", "Impressions": "7"}]


def test_snapshot_fetch_calls(monkeypatch):
    fake = Fake(monkeypatch)
    snap = audit_mod.fetch_snapshot(ACC, ("2026-10-16", "2026-10-18"))
    services = [c[1] for c in fake.calls]
    assert services.count("campaigns") == 1
    assert "chunk_key" not in next(c for c in fake.calls if c[1] == "campaigns")[5]
    for svc in ("adgroups", "ads", "keywords", "audiencetargets"):
        (call,) = [c for c in fake.calls if c[1] == svc]
        assert call[5]["chunk_key"] == "CampaignIds"
        # только ON-кампании; выключенная 999 в запрос не идёт
        assert sorted(call[5]["ids"]) == list(range(1, 13))
    ads = next(c for c in fake.calls if c[1] == "ads")
    assert "Title" in ads[4]["TextAdFieldNames"] and "SitelinkSetId" in ads[4]["TextAdFieldNames"]
    assert len(fake.reports) == 1
    rep = fake.reports[0]
    assert rep["ReportType"] == "CAMPAIGN_PERFORMANCE_REPORT"
    assert (rep["SelectionCriteria"]["DateFrom"], rep["SelectionCriteria"]["DateTo"]) == (
        "2026-10-16", "2026-10-18")
    assert snap.impressions_3d == {1: 7}
    assert snap.audiences_by_group == {10: 1}
    assert all(c["State"] == "ON" for c in snap.campaigns)


def test_report_window_is_three_days_ending_yesterday_msk(monkeypatch):
    fake = Fake(monkeypatch)
    audit_mod.run(_ctx())
    crit = fake.reports[0]["SelectionCriteria"]
    assert (crit["DateFrom"], crit["DateTo"]) == ("2026-10-16", "2026-10-18")


def test_unified_campaign_does_not_request_tracking_params(monkeypatch):
    fake = Fake(monkeypatch)
    audit_mod.fetch_snapshot(ACC, ("a", "b"))
    extra = fake.calls[0][4]
    assert "TrackingParams" in extra["TextCampaignFieldNames"]
    assert "TrackingParams" not in extra["UnifiedCampaignFieldNames"]


def test_no_campaigns_no_more_calls(monkeypatch):
    fake = Fake(monkeypatch, n_campaigns=0)
    out = audit_mod.run(_ctx())
    assert [c[1] for c in fake.calls] == ["campaigns"] and not fake.reports
    assert out.findings == [] and out.scanned_logins == ["login1"]


def test_failed_cabinet_not_in_scanned(monkeypatch):
    Fake(monkeypatch, fail_login="bad")
    bad = DirectAccount("bad", "t", "RUB", "B")
    out = audit_mod.run(_ctx(accounts=(ACC, bad)))
    assert out.scanned_logins == ["login1"]
    assert out.summary["errors"] and "bad" in out.summary["errors"][0]


def test_all_cabinets_failed_raises(monkeypatch):
    Fake(monkeypatch, fail_login="bad")
    with pytest.raises(RuntimeError):
        audit_mod.run(_ctx(accounts=(DirectAccount("bad", "t", "RUB", "B"),)))


def test_findings_never_carry_action(monkeypatch):
    Fake(monkeypatch)
    out = audit_mod.run(_ctx())
    assert out.findings and all(f.action is None for f in out.findings)
    assert out.summary["by_rule"]["no_negatives"] == 12


def test_audit_has_no_write_and_only_report_modes():
    assert not hasattr(audit_mod, "apply") and not hasattr(audit_mod, "rollback")
    assert ALLOWED_MODES["audit"] == ("off", "report")


def test_registered():
    from sync.modules.registry import MODULES
    assert MODULES["audit"] is audit_mod
