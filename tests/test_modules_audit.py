# -*- coding: utf-8 -*-
"""Сбор снимка audit и run(): формы запросов, отказ кабинета, режимы."""

from datetime import datetime, timezone

import pytest

from sync.modules import audit as audit_mod
from sync.modules.accounts import DirectAccount
from sync.modules.defaults import ALLOWED_MODES, settings_for
from sync.placements.direct import DirectError
from sync.modules.types import Context

NOW = datetime(2026, 10, 18, 22, 30, tzinfo=timezone.utc)  # 19.10 по Москве
ACC = DirectAccount("login1", "tok", "RUB", "X")


def _ctx(accounts=(ACC,), settings=None):
    return Context(slug="edunetwork", module_key="audit", mode="report",
                   settings=settings_for("audit", settings), accounts=list(accounts),
                   now=NOW, log=lambda m: None)


class Fake:
    def __init__(self, monkeypatch, n_campaigns=12, fail_login=None, reject=(), v501_fails=False,
                 rejected_fails=False):
        self.rejected_fails = rejected_fails
        self.reject = tuple(reject)
        self.v501_fails = v501_fails
        self.calls, self.reports = [], []
        self.n = n_campaigns
        self.fail_login = fail_login
        monkeypatch.setattr(audit_mod.direct_api, "get_all", self.get_all)
        monkeypatch.setattr(audit_mod.direct_api, "report_rows", self.report_rows)

    def get_all(self, token, login, service, criteria, fields, extra=None, **kw):
        self.calls.append((login, service, criteria, fields, extra, kw))
        if login == self.fail_login and service == "ads" and "Statuses" not in criteria:
            raise RuntimeError("Директ лёг")
        if service == "ads" and "Statuses" in criteria and self.rejected_fails:
            raise DirectError("ads [52] Сбой")
        if kw.get("version") == "v501":
            if self.v501_fails:
                raise DirectError("ads [8000] Неизвестный параметр ResponsiveAdFieldNames")
            return [{"Id": 300, "AdGroupId": 10, "CampaignId": 1, "Type": "RESPONSIVE_AD",
                     "ResponsiveAd": {"Titles": [{"Title": "Займы тут"}], "Texts": []}},
                    {"Id": 301, "CampaignId": 1, "Type": "TEXT_AD"}]
        if service == "campaigns":
            if "NegativeKeywordSharedSetIds" in fields and "NegativeKeywordSharedSetIds" in self.reject:
                raise DirectError("campaigns [4000] Неизвестное поле NegativeKeywordSharedSetIds")
            if "StartDate" in fields and "StartDate" in self.reject:
                raise DirectError("campaigns [4000] Неизвестное поле StartDate")
            unified = (extra or {}).get("UnifiedCampaignFieldNames") or []
            if "TrackingParams" in unified and "UnifiedTrackingParams" in self.reject:
                raise DirectError("campaigns [8000] Недопустимое поле TrackingParams")
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
    for svc in ("adgroups", "keywords", "audiencetargets"):
        (call,) = [c for c in fake.calls if c[1] == svc]
        assert call[5]["chunk_key"] == "CampaignIds"
        # только ON-кампании; выключенная 999 в запрос не идёт
        assert sorted(call[5]["ids"]) == list(range(1, 13))
    ads = [c for c in fake.calls if c[1] == "ads"]
    assert len(ads) == 3  # включённые, отклонённые, комбинаторные v501
    on, rejected, v501 = ads
    assert "Title" in on[4]["TextAdFieldNames"] and "SitelinkSetId" in on[4]["TextAdFieldNames"]
    # отклонённые — по Statuses и без фильтра State
    assert rejected[2] == {"Statuses": ["REJECTED"],
                           "States": ["ON", "OFF", "SUSPENDED", "OFF_BY_MONITORING"]}
    assert "ARCHIVED" not in rejected[2]["States"]
    assert v501[5]["version"] == "v501"
    assert v501[4]["ResponsiveAdFieldNames"] == ["Titles", "Texts"]
    assert [a["Id"] for a in snap.responsive_ads] == [300]
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


def test_optional_campaign_fields_requested_and_accepted(monkeypatch):
    fake = Fake(monkeypatch)
    audit_mod.fetch_snapshot(ACC, ("a", "b"))
    extra = fake.calls[0][4]
    assert "TrackingParams" in extra["TextCampaignFieldNames"]
    assert "TrackingParams" in extra["UnifiedCampaignFieldNames"]
    assert "NegativeKeywordSharedSetIds" in fake.calls[0][3]
    assert "StartDate" in fake.calls[0][3]


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


def test_rejected_shared_sets_field_falls_back(monkeypatch):
    fake = Fake(monkeypatch, reject=["NegativeKeywordSharedSetIds"])
    out = audit_mod.run(_ctx())
    camp_calls = [c for c in fake.calls if c[1] == "campaigns"]
    assert len(camp_calls) == 2
    assert "NegativeKeywordSharedSetIds" not in camp_calls[1][3]
    assert "TrackingParams" in camp_calls[1][4]["UnifiedCampaignFieldNames"]
    reason = "поле NegativeKeywordSharedSetIds не принято API"
    assert out.summary["skipped"]["no_negatives"] == {"count": 12, "reason": reason}
    assert "no_negatives" not in out.summary["by_rule"]
    assert "login1" in out.summary["unavailable"]
    assert out.scanned_logins == ["login1"]


def test_rejected_unified_tracking_field_falls_back(monkeypatch):
    fake = Fake(monkeypatch, reject=["UnifiedTrackingParams"])
    out = audit_mod.run(_ctx())
    camp_calls = [c for c in fake.calls if c[1] == "campaigns"]
    assert len(camp_calls) == 2
    assert "TrackingParams" not in camp_calls[1][4]["UnifiedCampaignFieldNames"]
    assert "NegativeKeywordSharedSetIds" in camp_calls[1][3]
    assert "no_negatives" in out.summary["by_rule"]  # поле принято — правило судит


def test_both_optional_fields_rejected(monkeypatch):
    fake = Fake(monkeypatch, reject=["NegativeKeywordSharedSetIds", "UnifiedTrackingParams"])
    snap = audit_mod.fetch_snapshot(ACC, ("a", "b"))
    assert len([c for c in fake.calls if c[1] == "campaigns"]) == 3
    assert set(snap.unavailable) == {"NegativeKeywordSharedSetIds", "UnifiedTrackingParams"}


def test_other_campaign_errors_are_not_swallowed(monkeypatch):
    def boom(*a, **k):
        raise DirectError("campaigns [53] Авторизация не прошла")
    monkeypatch.setattr(audit_mod.direct_api, "get_all", boom)
    with pytest.raises(RuntimeError):
        audit_mod.run(_ctx())


def test_v501_failure_keeps_cabinet_and_is_reported(monkeypatch):
    Fake(monkeypatch, v501_fails=True)
    out = audit_mod.run(_ctx(settings={"banned_words": ["кредит"]}))
    assert out.scanned_logins == ["login1"]
    assert "ResponsiveAds" in out.summary["unavailable"]["login1"]


def test_v501_comb_ad_hits_banned_word(monkeypatch):
    Fake(monkeypatch)
    out = audit_mod.run(_ctx(settings={"banned_words": ["займ"]}))
    assert [f.object_id for f in out.findings if f.rule == "banned_words"] == ["300"]


def test_summary_skipped_shape(monkeypatch):
    Fake(monkeypatch)
    out = audit_mod.run(_ctx())
    sk = out.summary["skipped"]
    assert sk["banned_words"]["reason"] == "список banned_words пуст — правило выключено"
    assert sk["strategy_goal_not_allowed"] == {
        "count": 12, "reason": "список allowed_goal_ids пуст — правило выключено"}


def test_rejected_request_failure_is_isolated(monkeypatch):
    Fake(monkeypatch, rejected_fails=True)
    out = audit_mod.run(_ctx())
    assert out.scanned_logins == ["login1"]
    assert "RejectedAds" in out.summary["unavailable"]["login1"]
    assert out.summary["skipped"]["ads_rejected"]["count"] == 1
    assert out.summary["by_rule"]["no_negatives"] == 12  # остальные правила работают


def test_start_date_is_optional_field(monkeypatch):
    fake = Fake(monkeypatch, reject=["StartDate"])
    out = audit_mod.run(_ctx())
    camp_calls = [c for c in fake.calls if c[1] == "campaigns"]
    assert len(camp_calls) == 2 and "StartDate" not in camp_calls[1][3]
    # остальные необязательные поля при этом не потеряны
    assert "NegativeKeywordSharedSetIds" in camp_calls[1][3]
    assert out.summary["skipped"]["on_no_impressions"] == {
        "count": 12, "reason": "поле StartDate не принято API"}
    assert out.scanned_logins == ["login1"]
