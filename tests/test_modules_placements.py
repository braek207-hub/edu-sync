# -*- coding: utf-8 -*-
"""Модуль placements: находки из плана, запись с чтением обратно, откат, старый CLI."""

import dataclasses
from datetime import datetime, timezone

import pytest

from sync import placements_clean
from sync.modules import placements as pm
from sync.modules.accounts import DirectAccount
from sync.modules.defaults import settings_for
from sync.modules.types import Context, StoredFinding, fingerprint
from sync.placements import direct
from sync.placements import plan as planner

NOW = datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)
ACC = DirectAccount("login1", "tok", "RUB", "X")
SITES = ["com.aaa.bbb", "com.ccc.ddd", "com.eee.fff"]


def _ctx(settings=None, accounts=(ACC,)):
    return Context(slug="edunetwork", module_key="placements", mode="report",
                   settings=settings_for("placements", settings),
                   accounts=list(accounts), now=NOW, log=lambda m: None)


def _camp(cid, items=(), state="ON", ctype="TEXT_CAMPAIGN"):
    return {"Id": cid, "Name": "К%d" % cid, "Type": ctype, "State": state,
            "ExcludedSites": {"Items": list(items)}}


def _rows(cids, sites=SITES):
    return [{"campaign_id": str(c), "placement": s, "clicks": 10 + i, "cost": 5.0}
            for c in cids for i, s in enumerate(sites)]


@pytest.fixture
def api(monkeypatch):
    state = {"campaigns": [_camp(1), _camp(2)], "rows": _rows([1, 2]), "today_calls": 0}

    def today(token, login):
        state["today_calls"] += 1
        return state["rows"]

    monkeypatch.setattr(direct, "campaigns", lambda t, l: state["campaigns"])
    monkeypatch.setattr(direct, "placements_today", today)
    monkeypatch.setattr(direct, "campaign_goals", lambda t, l: {})
    monkeypatch.setattr(direct, "placements_with_goals", lambda t, l, g: [])
    return state


def test_run_maps_plan_actions_to_findings(api):
    out = pm.run(_ctx())
    assert len(out.findings) == 6
    assert out.scanned_logins == ["login1"]
    for f in out.findings:
        assert f.object_type == "site" and f.rule == "app"
        assert f.action == {"op": "exclude_site", "campaign_id": f.campaign_id,
                            "site": f.object_id}
        assert fingerprint("placements", f) == "placements:app:login1:%d:%s" % (
            f.campaign_id, f.object_id)
        assert set(f.details) == {"clicks", "cost", "reason", "fill_after"}


def test_login_without_live_campaigns_orders_no_report(api):
    api["campaigns"] = [_camp(1, state="SUSPENDED")]
    out = pm.run(_ctx())
    assert out.findings == [] and api["today_calls"] == 0
    assert out.scanned_logins == ["login1"]


def test_settings_feed_planner(api, monkeypatch):
    seen = {}
    real = planner.plan_account

    def spy(rows, campaigns, **kw):
        seen.update(kw)
        return real(rows, campaigns, **kw)

    monkeypatch.setattr(planner, "plan_account", spy)
    monkeypatch.setattr(pm.spam, "suspicious_sites", lambda w, g: {"spam.ru": "аномалия"})
    monkeypatch.setattr(direct, "campaign_goals", lambda t, l: {"1": 7})
    out = pm.run(_ctx({"top_n": 5, "always_block": ["Mail.ru"], "spam_rule": True,
                       "extra_allow": ["com.aaa.bbb"]}))
    assert seen["top_n"] == 5
    assert seen["always_block"] == ["mail.ru"]
    assert seen["spam"] == {"spam.ru": "аномалия"}
    assert "com.aaa.bbb" in seen["allow_exact"]
    objs = {f.object_id for f in out.findings}
    assert "com.aaa.bbb" not in objs
    assert {"mail.ru", "spam.ru"} <= objs
    assert {f.rule for f in out.findings if f.object_id == "mail.ru"} == {"forced"}


def test_refusals_in_summary(api):
    api["campaigns"] = [_camp(1), _camp(2, items=["x%d.ru" % i for i in range(900)])]
    api["rows"] = _rows([1, 2, 3])  # 3 — Мастер кампаний, API её не отдаёт
    out = pm.run(_ctx())
    refused = out.summary["refused"]
    assert any("Мастер кампаний" in r and "кл" in r and "₽" in r for r in refused)
    assert any("список запретов заполнен" in r for r in refused)
    assert all(f.campaign_id == 1 for f in out.findings)


class FakeDirect:
    """Директ в памяти: списки запретов по кампаниям, учёт записей."""

    def __init__(self, monkeypatch, campaigns, drop=()):
        self.c = {c["Id"]: c for c in campaigns}
        self.writes = []
        self.drop = set(drop)  # площадки, которые Директ «принимает» и не сохраняет
        monkeypatch.setattr(pm.direct_api, "get_all", self.get_all)
        monkeypatch.setattr(pm.direct_api, "update_campaign_list", self.update)
        monkeypatch.setattr(pm.direct_api, "read_campaign_list", self.read)

    def get_all(self, token, login, service, criteria, fields, **kw):
        c = self.c.get(criteria["Ids"][0])
        return [c] if c else []

    def update(self, token, login, cid, field, items):
        self.writes.append((cid, None if items is None else list(items)))
        items = items or []  # None — поле очищено целиком
        self.c[cid]["ExcludedSites"] = {"Items": [i for i in items if i not in self.drop]}
        return {"Id": cid}

    def read(self, token, login, cid, field):
        return list(self.c[cid]["ExcludedSites"]["Items"])


def _stored(i, site, cid=1):
    return StoredFinding(
        id=i, status="approved", fingerprint="placements:app:login1:%d:%s" % (cid, site),
        last_seen_at=NOW, rule="app", severity="info", title="t", login="login1",
        campaign_id=cid, campaign_name="К", object_type="site", object_id=site,
        details={}, action={"op": "exclude_site", "campaign_id": cid, "site": site})


def test_apply_groups_by_campaign_and_reads_back(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(1)], drop={SITES[2]})
    res = pm.apply(_ctx(), [_stored(i, s) for i, s in enumerate(SITES, 1)])
    assert len(fake.writes) == 1
    assert res[1].ok and res[2].ok
    assert not res[3].ok and res[3].error == "Директ принял, но не применил"


def test_apply_keeps_human_items(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(1, items=["Ручной.ru"])])
    res = pm.apply(_ctx(), [_stored(1, SITES[0])])
    assert res[1].ok
    assert fake.writes[0][1] == ["Ручной.ru", SITES[0]]


def test_apply_campaign_off_or_full_fails_with_reason(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(1, state="OFF"),
                                    _camp(2, items=["x%d.ru" % i for i in range(900)])])
    off = pm.apply(_ctx(), [_stored(1, SITES[0], cid=1)])
    full = pm.apply(_ctx(), [_stored(2, SITES[0], cid=2)])
    assert not off[1].ok and "кампания не запущена" in off[1].error
    assert not full[2].ok and "список запретов заполнен" in full[2].error
    assert fake.writes == []


def test_rollback_removes_only_own(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(1, items=["Ручной.ru", SITES[0], SITES[1]])])
    res = pm.rollback(_ctx(), [_stored(1, SITES[0])])
    assert res[1].ok
    assert fake.writes == [(1, ["Ручной.ru", SITES[1]])]
    assert fake.c[1]["ExcludedSites"]["Items"] == ["Ручной.ru", SITES[1]]


def test_rollback_fails_when_site_stays(monkeypatch):
    class Stubborn(FakeDirect):
        def update(self, token, login, cid, field, items):
            self.writes.append((cid, list(items)))
            return {"Id": cid}  # Директ ответил успехом, список не изменил

    Stubborn(monkeypatch, [_camp(1, items=[SITES[0]])])
    res = pm.rollback(_ctx(), [_stored(1, SITES[0])])
    assert not res[1].ok


def test_module_registered():
    from sync.modules.registry import MODULES
    assert MODULES["placements"] is pm


def test_apply_site_already_present_is_not_ours(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(1, items=[SITES[0]])])
    res = pm.apply(_ctx(), [_stored(1, SITES[0])])
    assert not res[1].ok and res[1].response["already_present"] is True
    assert "запрет не робота" in res[1].error
    assert fake.writes == []


def test_rollback_keeps_human_case_variant(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(1, items=["COM.AAA.BBB", "Ручной.ru"])])
    res = pm.rollback(_ctx(), [_stored(1, "com.aaa.bbb")])
    assert not res[1].ok and "другом регистре" in res[1].error
    assert fake.writes == []
    assert fake.c[1]["ExcludedSites"]["Items"] == ["COM.AAA.BBB", "Ручной.ru"]


def test_rollback_site_absent_is_ok_without_write(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(1, items=["Ручной.ru"])])
    res = pm.rollback(_ctx(), [_stored(1, SITES[0])])
    assert res[1].ok and fake.writes == []


def test_write_timeout_but_direct_took_it_is_ok(monkeypatch):
    class Timeout(FakeDirect):
        def update(self, token, login, cid, field, items):
            super().update(token, login, cid, field, items)
            raise TimeoutError("timed out")

    Timeout(monkeypatch, [_camp(1)])
    res = pm.apply(_ctx(), [_stored(1, SITES[0])])
    assert res[1].ok


def test_write_error_and_readback_error_fails(monkeypatch):
    class Dead(FakeDirect):
        def update(self, token, login, cid, field, items):
            raise TimeoutError("timed out")

        def read(self, token, login, cid, field):
            raise ConnectionError("down")

    Dead(monkeypatch, [_camp(1)])
    with pytest.raises(TimeoutError):
        pm._apply_group(ACC, 1, [_stored(1, SITES[0])])
    res = pm.apply(_ctx(), [_stored(1, SITES[0])])
    assert not res[1].ok and "TimeoutError" in res[1].error


def test_write_error_site_missing_reports_the_error(monkeypatch):
    class Refuse(FakeDirect):
        def update(self, token, login, cid, field, items):
            raise direct.DirectError("5005 bad")

    Refuse(monkeypatch, [_camp(1)])
    res = pm.apply(_ctx(), [_stored(1, SITES[0])])
    assert not res[1].ok and "5005 bad" in res[1].error


def test_rollback_to_empty_list_sends_null(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(1, items=[SITES[0]])])
    res = pm.rollback(_ctx(), [_stored(1, SITES[0])])
    assert res[1].ok
    assert fake.writes == [(1, None)]


def test_update_campaign_list_null_payload():
    from sync.modules import direct_api
    sent = []
    direct_api.update_campaign_list(
        "t", "l", 5, "ExcludedSites", None,
        call_fn=lambda *a: sent.append(a[3]) or {"UpdateResults": [{"Id": 5}]})
    assert sent[0]["params"]["Campaigns"] == [{"Id": 5, "ExcludedSites": None}]


def test_readback_none_has_own_reason(monkeypatch):
    class Gone(FakeDirect):
        def read(self, token, login, cid, field):
            return None

    Gone(monkeypatch, [_camp(1)])
    res = pm.apply(_ctx(), [_stored(1, SITES[0])])
    assert res[1].error == "кампания не найдена при чтении обратно"


def test_failed_spam_report_keeps_login_out_of_scanned(api, monkeypatch):
    monkeypatch.setattr(direct, "campaign_goals", lambda t, l: (_ for _ in ()).throw(
        direct.DirectError("204")))
    out = pm.run(_ctx({"spam_rule": True}))
    assert out.scanned_logins == []
    assert len(out.findings) == 6  # чистка не блокируется
    assert any("спам-правило" in e for e in out.summary["errors"])


def test_failed_model_keeps_login_out_of_scanned(api, monkeypatch):
    api["rows"] = _rows([1], sites=["blog.example.com"])
    monkeypatch.setenv(pm.LLM_ENV, "1")
    monkeypatch.setattr(pm.llm, "asker", lambda: (lambda prompt: 1 / 0))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    out = pm.run(_ctx())
    assert out.scanned_logins == []
    assert any("модель" in e for e in out.summary["errors"])


def _cli(monkeypatch):
    calls = []
    monkeypatch.setattr(placements_clean, "run_project",
                        lambda slug, **kw: calls.append((slug, kw)) or 0)
    return calls


def test_old_cli_delegates_to_runner(monkeypatch):
    calls = _cli(monkeypatch)
    assert placements_clean.main(["--apply"]) == 0
    assert calls[0][0] == "edunetwork" and calls[0][1]["only"] == "placements"
    assert calls[0][1]["dry_run"] is False
    assert placements_clean.main([]) == 0
    assert calls[1][1]["dry_run"] is True


def test_unban_writes_only_with_apply(monkeypatch, capsys):
    calls = _cli(monkeypatch)
    asked = []
    monkeypatch.setattr(placements_clean.store, "request_rollback",
                        lambda *a, **k: asked.append(a) or 2)
    monkeypatch.setattr(placements_clean.store, "existing_findings",
                        lambda slug, mod: [
                            dataclasses.replace(_stored(1, "a.ru"), status="applied"),
                            dataclasses.replace(_stored(2, "b.ru"), status="applied"),
                            _stored(3, "a.ru", cid=2)])  # approved: в список не попадает
    monkeypatch.setattr(placements_clean.store, "load_project_modules",
                        lambda slug: [{"module_key": "placements", "mode": "report"}])
    assert placements_clean.main(["--unban", "Com.Block/"]) == 0
    assert placements_clean.main(["--unban", "a.ru", "--dry-run"]) == 0
    assert asked == []
    shown = capsys.readouterr().out
    assert "login1 / кампания 1 / a.ru" in shown
    assert "кампания 2" not in shown and "b.ru" not in shown.split("затронуло бы:")[1]
    assert "итого: 1" in shown

    assert placements_clean.main(["--unban", "A.ru", "--apply"]) == 0
    assert asked == [("edunetwork", "placements", ["a.ru"])]
    assert "раннер не выполнит откат" in capsys.readouterr().out
    assert calls == []


def test_unban_dry_run_db_failure_is_a_clear_line(monkeypatch, capsys):
    def boom(slug, mod):
        raise RuntimeError("нет соединения")

    monkeypatch.setattr(placements_clean.store, "existing_findings", boom)
    assert placements_clean.main(["--unban", "a.ru"]) == 1
    out = capsys.readouterr().out
    assert "база недоступна" in out and "нет соединения" in out
