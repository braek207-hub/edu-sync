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


def _ctx(settings=None, accounts=(ACC,), project_goals=(), progress=None):
    extra = {"progress": progress} if progress else {}
    return Context(slug="edunetwork", module_key="placements", mode="report",
                   settings=settings_for("placements", settings),
                   accounts=list(accounts), now=NOW, log=lambda m: None,
                   project_goals=list(project_goals), **extra)


def _camp(cid, items=(), state="ON", ctype="TEXT_CAMPAIGN"):
    return {"Id": cid, "Name": "К%d" % cid, "Type": ctype, "State": state,
            "ExcludedSites": {"Items": list(items)}}


def _rows(cids, sites=SITES):
    return [{"campaign_id": str(c), "placement": s, "clicks": 10 + i, "cost": 5.0}
            for c in cids for i, s in enumerate(sites)]


@pytest.fixture
def api(monkeypatch):
    state = {"campaigns": [_camp(1), _camp(2)], "rows": _rows([1, 2]), "today_calls": 0,
             "window": {}, "window_calls": [], "window_fail": set(),
             "campaigns_by": {}, "window_by": {}}

    def today(token, login):
        state["today_calls"] += 1
        return state["rows"]

    def window(token, login, days, goals):
        state["window_calls"].append((login, days, list(goals)))
        if isinstance(state["window"], Exception):
            raise state["window"]
        if login in state["window_fail"]:
            raise direct.DirectError("отчёт не готов")
        return state["window_by"].get(login, state["window"]).get(days, [])

    monkeypatch.setattr(direct, "campaigns",
                        lambda t, l: state["campaigns_by"].get(l, state["campaigns"]))
    monkeypatch.setattr(direct, "placements_today", today)
    monkeypatch.setattr(direct, "campaign_goals", lambda t, l: {})
    monkeypatch.setattr(direct, "placements_with_goals", lambda t, l, g: [])
    monkeypatch.setattr(direct, "placements_window", window)
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
        # ключи этапа 1 на месте (по ним рисуются старые находки), плюс снимок этапа 2
        assert set(f.details) == {"clicks", "cost", "reason", "fill_after", "rule",
                                  "matched", "window", "metrics", "list_fill"}


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
    monkeypatch.setattr(pm.spam, "suspicious_sites", lambda w, g, **kw: {"spam.ru": "аномалия"})
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
    assert res[1].error == "кампания не найдена в Директе после записи"


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
    # в report откат выполняется: предупреждать не о чем
    assert "выключен" not in capsys.readouterr().out
    monkeypatch.setattr(placements_clean.store, "load_project_modules", lambda slug: [])
    assert placements_clean.main(["--unban", "A.ru", "--apply"]) == 0
    assert "выключен — откат выполнится после включения" in capsys.readouterr().out
    assert calls == []


def test_unban_dry_run_db_failure_is_a_clear_line(monkeypatch, capsys):
    def boom(slug, mod):
        raise RuntimeError("нет соединения")

    monkeypatch.setattr(placements_clean.store, "existing_findings", boom)
    assert placements_clean.main(["--unban", "a.ru"]) == 1
    out = capsys.readouterr().out
    assert "база недоступна" in out and "нет соединения" in out


def test_second_finding_same_site_same_campaign_is_a_named_duplicate(monkeypatch):
    # одна площадка под двумя правилами в одной кампании: запись одна, вторая — дубль
    fake = FakeDirect(monkeypatch, [_camp(1)])
    second = dataclasses.replace(_stored(2, SITES[0]), rule="spam",
                                 fingerprint="placements:spam:login1:1:%s" % SITES[0])
    res = pm.apply(_ctx(), [_stored(1, SITES[0]), second])
    assert res[1].ok
    assert not res[2].ok and "дубль" in res[2].error
    assert fake.writes == [(1, [SITES[0]])]


# --- этап 2: правила, словарь, пороги спама ---------------------------------------

RID = "aaa111"
G = 111


def _rule(conds, rid=RID, action="block", window=0, where="seen", only=(), except_=(),
          enabled=True):
    return {"id": rid, "name": "Правило %s" % rid, "enabled": enabled, "action": action,
            "window": window, "where": where,
            "campaigns": {"only": list(only), "except": list(except_)},
            "conditions": [{"field": f, "op": o, "value": v} for f, o, v in conds]}


def _day(cid, site, clicks, cost, impressions=None):
    return {"campaign_id": str(cid), "placement": site, "clicks": clicks, "cost": cost,
            "impressions": clicks * 10 if impressions is None else impressions}


def _win(cid, site, clicks, cost, leads=None, impressions=None):
    row = _day(cid, site, clicks, cost, impressions)
    row["conversions"] = {} if leads is None else {G: leads}
    return row


def _codes(out):
    return [r for r in out.summary["refused"] if isinstance(r, dict)]


def _refused(out, code, **match):
    return [r for r in _codes(out)
            if r["code"] == code and all(r.get(k) == v for k, v in match.items())]


def test_disabled_category_reaches_user_rule(api):
    api["rows"] = [_day(1, "games.example.ru", 12, 30.0)]
    rule = _rule([("category", "is", "game"), ("clicks", "gte", 10)])
    out = pm.run(_ctx({"dictionary": {"categories": ["app", "dsp", "junk"]},
                       "rules": [rule]}))
    assert [(f.rule, f.campaign_id, f.object_id) for f in out.findings] == [
        ("u:" + RID, 1, "games.example.ru")]
    assert fingerprint("placements", out.findings[0]) == \
        "placements:u:%s:login1:1:games.example.ru" % RID
    # выключенная категория не закрывает свои прошлые находки
    assert ("login1", "game") in out.unverified
    assert out.summary["disabled"] == ["game", "spam"]

    # включённая категория действует раньше правила, правило — «сработало также»
    on = pm.run(_ctx({"rules": [rule]}))
    assert [f.rule for f in on.findings] == ["game"]
    assert [m["id"] for m in on.findings[0].details["matched"]] == ["game", "u:" + RID]


def test_extra_allow_beats_stat_rule(api):
    api["rows"] = [_day(1, "good.example.ru", 50, 300.0)]
    out = pm.run(_ctx({"extra_allow": ["Good.example.ru"],
                       "rules": [_rule([("clicks", "gte", 10)])]}))
    assert out.findings == []
    assert _refused(out, "in_never_list", object="good.example.ru", campaign_id=1,
                    rule="u:" + RID)


def test_system_allowlist_beats_rule_by_stats(api):
    api["rows"] = []
    api["window"] = {7: [_win(1, "avito.ru", 100, 1000.0, leads=30),
                         _win(1, "plain.example.ru", 1000, 900.0, leads=10)]}
    out = pm.run(_ctx({"rules": [_rule([("cr_x", "gte", 3)], window=7)]},
                      project_goals=[G]))
    assert out.findings == []
    assert _refused(out, "system_allowlist", object="avito.ru", campaign_id=1,
                    rule="u:" + RID)
    assert api["window_calls"] == [("login1", 7, [G])]


def test_max_per_run_keeps_most_expensive(api):
    sites = {"alpha.ru": 10.0, "beta.ru": 50.0, "gamma.ru": 30.0, "delta.ru": 40.0,
             "omega.ru": 20.0}
    api["rows"] = [_day(1, s, 1, c) for s, c in sites.items()]
    out = pm.run(_ctx({"max_per_run": 2, "rules": [_rule([("cost", "gte", 1)])]}))
    assert [f.object_id for f in out.findings] == ["beta.ru", "delta.ru"]
    assert out.summary["by_rule"]["u:" + RID] == {"found": 2, "refused": 0, "over_cap": 3}
    assert out.summary["over_cap"] == 3


def test_max_per_run_does_not_cap_builtins(api):
    # 6 находок словаря при лимите 1: лимит только для правил пользователя
    out = pm.run(_ctx({"max_per_run": 1}))
    assert len(out.findings) == 6 and {f.rule for f in out.findings} == {"app"}


def test_account_rule_closes_all_eligible_campaigns(api):
    api["campaigns"] = [_camp(1), _camp(2), _camp(3, state="SUSPENDED"),
                        _camp(4, items=["Bad.example.ru"]),
                        _camp(5, ctype="SMART_CAMPAIGN")]
    api["rows"] = []
    api["window"] = {7: [_win(1, "bad.example.ru", 20, 100.0)]}
    rule = _rule([("name", "equals", "bad.example.ru"), ("clicks", "gte", 10)],
                 window=7, where="account")
    out = pm.run(_ctx({"rules": [rule]}))
    assert sorted(f.campaign_id for f in out.findings) == [1, 2]
    assert {f.rule for f in out.findings} == {"u:" + RID}
    # без целей-лидов отчёт всё равно заказан — правилу по кликам цели не нужны
    assert api["window_calls"] == [("login1", 7, [])]

    only = pm.run(_ctx({"rules": [dict(rule, campaigns={"only": [], "except": [2]})]}))
    assert [f.campaign_id for f in only.findings] == [1]


def test_lead_rule_without_visible_goal_is_unverified(api):
    api["rows"] = []
    api["window"] = {7: [_win(1, "x1.example.ru", 10, 100.0, leads=0),
                         _win(1, "lead.example.ru", 10, 50.0, leads=3),
                         _win(2, "x2.example.ru", 10, 100.0, leads=0)]}
    out = pm.run(_ctx({"rules": [_rule([("leads", "eq", 0), ("clicks", "gte", 5)],
                                       window=7)]}, project_goals=[G]))
    assert [(f.campaign_id, f.object_id) for f in out.findings] == [(1, "x1.example.ru")]
    assert ("login1", "u:" + RID, 2) in out.unverified
    # расход кампании за окно — для текста «0 лидов при расходе X»
    assert _refused(out, "goal_not_visible", campaign_id=2, rule="u:" + RID, object=None,
                    cost=100.0)


def test_account_rule_invisible_goal_cost_is_of_invisible_campaigns(api):
    # строка по логину не проверена из-за кампании 2: расход в отказе — её, не логина
    api["rows"] = []
    api["window"] = {7: [_win(1, "x1.example.ru", 10, 100.0, leads=3),
                         _win(2, "x1.example.ru", 10, 40.0, leads=0),
                         _win(2, "x2.example.ru", 10, 60.0, leads=0)]}
    rule = _rule([("leads", "eq", 0)], window=7, where="account")
    out = pm.run(_ctx({"rules": [rule]}, project_goals=[G]))
    assert out.findings == []
    assert _refused(out, "goal_not_visible", rule="u:" + RID, campaign_id=None, cost=100.0)


def test_lead_rule_without_lead_goals_refused_for_login(api):
    api["rows"] = []
    api["window"] = {7: [_win(1, "x1.example.ru", 10, 100.0)]}
    out = pm.run(_ctx({"rules": [_rule([("leads", "eq", 0)], window=7)]}))
    assert out.findings == []
    assert ("login1", "u:" + RID) in out.unverified
    assert _refused(out, "no_lead_goals", rule="u:" + RID, campaign_id=None)


def test_non_rub_cabinet_refuses_money_rule(api):
    api["rows"] = [_day(1, "beta.ru", 10, 500.0)]
    kzt = DirectAccount("login1", "tok", "KZT", "X")
    out = pm.run(_ctx({"rules": [_rule([("cost", "gte", 100)])]}, accounts=(kzt,)))
    assert out.findings == []
    assert _refused(out, "non_rub_money", rule="u:" + RID)
    assert ("login1", "u:" + RID) in out.unverified


def test_user_finding_details(api):
    api["rows"] = []
    api["window"] = {7: [_win(1, "beta.ru", 20, 200.0, leads=2, impressions=400)]}
    out = pm.run(_ctx({"rules": [_rule([("clicks", "gte", 10)], action="propose",
                                       window=7)]}, project_goals=[G]))
    (f,) = out.findings
    assert f.rule == "u:" + RID
    assert f.action == {"op": "exclude_site", "campaign_id": 1, "site": "beta.ru"}
    d = f.details
    assert {"rule", "matched", "window", "metrics", "list_fill"} <= set(d)
    assert d["propose"] is True
    assert d["rule"] == {"id": "u:" + RID, "name": "Правило " + RID, "action": "propose",
                         "window": 7, "where": "seen",
                         "conditions": [{"field": "clicks", "op": "gte", "value": 10}]}
    assert d["matched"] == [{"id": "u:" + RID, "name": "Правило " + RID,
                             "conds": [{"field": "clicks", "op": "gte", "value": 10,
                                        "actual": 20.0}]}]
    # NOW 09:00 UTC = 12:00 МСК 18.10; LAST_7_DAYS сегодня не включает
    assert d["window"] == {"from": "2026-10-11", "to": "2026-10-17"}
    assert d["metrics"] == {"clicks": 20, "cost": 200.0, "impressions": 400, "leads": 2,
                            "cr": 10.0, "cpl": 100.0, "account_cr": 10.0,
                            "account_cpl": 100.0}
    assert d["list_fill"] == {"before": 0, "limit": direct.MAX_EXCLUDED_SITES}


def test_block_rule_has_no_propose_flag(api):
    api["rows"] = [_day(1, "beta.ru", 20, 200.0)]
    (f,) = pm.run(_ctx({"rules": [_rule([("clicks", "gte", 10)])]})).findings
    assert "propose" not in f.details
    assert f.details["window"] == {"from": "2026-10-18", "to": "2026-10-18"}


def test_builtin_app_fingerprint_is_stage_one(api):
    # регрессия перенесённых находок этапа 1: правило рядом не меняет fingerprint
    rule = _rule([("name", "starts", "com.")])
    for settings in (None, {"rules": [rule]}):
        out = pm.run(_ctx(settings))
        assert sorted(fingerprint("placements", f) for f in out.findings) == sorted(
            "placements:app:login1:%d:%s" % (c, s) for c in (1, 2) for s in SITES)
    f = out.findings[0]
    assert f.details["rule"] == {"id": "app", "name": "Словарь мусора", "builtin": True,
                                 "params": {"categories": ["app", "dsp", "game", "junk"],
                                            "top_n": 30}}
    assert [m["id"] for m in f.details["matched"]] == ["app", "u:" + RID]


def _spam_week(monkeypatch):
    monkeypatch.setattr(direct, "campaign_goals", lambda t, l: {"1": 7})
    monkeypatch.setattr(direct, "placements_with_goals", lambda t, l, g: [
        {"campaign_id": "1", "placement": "lucky.example.ru", "clicks": 20,
         "conversions": {7: 4}},
        {"campaign_id": "1", "placement": "other.example.ru", "clicks": 400,
         "conversions": {7: 4}}])


def test_spam_thresholds_from_settings(api, monkeypatch):
    _spam_week(monkeypatch)
    default = pm.run(_ctx({"spam_rule": True}))
    assert not [f for f in default.findings if f.rule == "spam"]
    low = pm.run(_ctx({"spam_rule": True, "spam": {"min_leads": 3}}))
    spam_f = [f for f in low.findings if f.rule == "spam"]
    assert sorted(f.campaign_id for f in spam_f) == [1, 2]
    assert {f.object_id for f in spam_f} == {"lucky.example.ru"}
    assert spam_f[0].details["rule"]["params"]["min_leads"] == 3


def test_suspicious_sites_threshold_kwargs():
    rows = [{"campaign_id": "1", "placement": "lucky.example.ru", "clicks": 20,
             "conversions": {7: 4}},
            {"campaign_id": "1", "placement": "other.example.ru", "clicks": 400,
             "conversions": {7: 4}}]
    assert pm.spam.suspicious_sites(rows, {"1": 7}) == {}
    assert set(pm.spam.suspicious_sites(rows, {"1": 7}, min_leads=3)) == {"lucky.example.ru"}
    # CR площадки 20 %: при пороге 25 % уже не спам
    assert pm.spam.suspicious_sites(rows, {"1": 7}, min_leads=3, min_cr=25) == {}


def test_spam_on_lead_goals_shares_rule_report(api, monkeypatch):
    called = []
    monkeypatch.setattr(direct, "campaign_goals", lambda t, l: called.append(1) or {"1": 7})
    api["window"] = {7: [_win(1, "lucky.example.ru", 20, 10.0, leads=6),
                         _win(1, "other.example.ru", 400, 10.0, leads=4)]}
    out = pm.run(_ctx({"spam_rule": True, "spam": {"goals": "leads"},
                       "rules": [_rule([("clicks", "gte", 1000)], window=7)]},
                      project_goals=[G]))
    assert called == []
    assert api["window_calls"] == [("login1", 7, [G])]
    assert {f.object_id for f in out.findings if f.rule == "spam"} == {"lucky.example.ru"}


def test_user_protect_beats_dictionary_and_spam(api, monkeypatch):
    monkeypatch.setattr(pm.spam, "suspicious_sites",
                        lambda w, g, **kw: {"spam.example.ru": "аномалия"})
    monkeypatch.setattr(direct, "campaign_goals", lambda t, l: {"1": 7})
    protect = _rule([("name", "equals", SITES[0])], rid="ppp111", action="protect")
    guard_spam = _rule([("name", "equals", "spam.example.ru")], rid="ppp222",
                       action="protect", only=[1])
    out = pm.run(_ctx({"spam_rule": True, "rules": [protect, guard_spam]}))
    objs = {(f.campaign_id, f.object_id) for f in out.findings}
    assert not {o for o in objs if o[1] == SITES[0]}
    # защита only=[1] держит спам только в кампании 1
    assert (2, "spam.example.ru") in objs and (1, "spam.example.ru") not in objs
    assert _refused(out, "user_protect", rule="app", object=SITES[0], by="u:ppp111")
    assert _refused(out, "user_protect", rule="spam", campaign_id=1, by="u:ppp222")
    assert out.summary["by_rule"]["u:ppp111"]["found"] == 2


def test_unverifiable_protect_holds_row(api):
    api["rows"] = []
    api["window"] = {7: [_win(1, "beta.ru", 20, 200.0, leads=0)]}
    protect = _rule([("leads", "gte", 1)], rid="ppp111", action="protect", window=7)
    block = _rule([("clicks", "gte", 10)], window=7)
    out = pm.run(_ctx({"rules": [protect, block]}, project_goals=[G]))
    assert out.findings == []
    assert ("login1", "u:" + RID, 1) in out.unverified
    assert _refused(out, "goal_not_visible", rule="u:" + RID, by="u:ppp111", held=True,
                    cost=200.0)


def test_never_list_beats_unverifiable_protect(api):
    # твёрдая защита решает раньше удержания: без лишней тройки unverified
    api["rows"] = []
    api["window"] = {7: [_win(1, "beta.ru", 20, 200.0, leads=0)]}
    protect = _rule([("leads", "gte", 1)], rid="ppp111", action="protect", window=7)
    block = _rule([("clicks", "gte", 10)], window=7)
    out = pm.run(_ctx({"extra_allow": ["beta.ru"], "rules": [protect, block]},
                      project_goals=[G]))
    assert out.findings == []
    assert _refused(out, "in_never_list", object="beta.ru", campaign_id=1, rule="u:" + RID)
    assert not _refused(out, "goal_not_visible", held=True)
    assert ("login1", "u:" + RID, 1) not in out.unverified


def test_account_rule_only_is_limited_to_own_campaigns(api):
    # only — список на проект: кампания 1 у login1, login2 о ней не отказывает
    api["campaigns_by"] = {"login1": [_camp(1)], "login2": [_camp(7)]}
    api["rows"] = []
    api["window_by"] = {"login1": {7: [_win(1, "bad.example.ru", 20, 100.0)]},
                        "login2": {7: [_win(7, "bad.example.ru", 20, 100.0)]}}
    rule = _rule([("clicks", "gte", 10)], window=7, where="account", only=[1])
    accs = (ACC, DirectAccount("login2", "t", "RUB", "Y"))
    out = pm.run(_ctx({"rules": [rule]}, accounts=accs))
    assert [(f.login, f.campaign_id) for f in out.findings] == [("login1", 1)]
    assert not [r for r in _codes(out) if r.get("login") == "login2"]
    assert out.scanned_logins == ["login1", "login2"]


def test_account_rule_only_names_unusable_campaigns(api):
    api["campaigns"] = [_camp(1), _camp(3, state="SUSPENDED"),
                        _camp(5, ctype="SMART_CAMPAIGN")]
    api["rows"] = []
    api["window"] = {7: [_win(1, "bad.example.ru", 20, 100.0)]}
    rule = _rule([("clicks", "gte", 10)], window=7, where="account", only=[1, 3, 5])
    out = pm.run(_ctx({"rules": [rule]}))
    assert [f.campaign_id for f in out.findings] == [1]
    assert _refused(out, "campaign_stopped", rule="u:" + RID, campaign_id=3)
    assert _refused(out, "campaign_type_unsupported", rule="u:" + RID, campaign_id=5)


def test_protect_report_failure_fails_login(api):
    # не зная защит, логин не закрывает ничего; соседний логин работает
    api["window_fail"] = {"login1"}
    protect = _rule([("clicks", "gte", 10)], rid="ppp111", action="protect", window=7)
    accs = (ACC, DirectAccount("login2", "t", "RUB", "Y"))
    out = pm.run(_ctx({"rules": [protect]}, accounts=accs))
    assert {f.login for f in out.findings} == {"login2"}
    assert len(out.findings) == 6
    assert out.scanned_logins == ["login2"]
    assert len(out.summary["errors"]) == 1
    assert out.summary["errors"][0].startswith("login1:")
    assert "защит правил" in out.summary["errors"][0]
    # упали все логины — прогон падает целиком
    api["window_fail"] = {"login1", "login2"}
    with pytest.raises(RuntimeError, match="защит правил"):
        pm.run(_ctx({"rules": [protect]}, accounts=accs))


def test_rule_report_failure_keeps_builtins_but_not_scanned(api):
    api["window"] = direct.DirectError("отчёт не готов")
    out = pm.run(_ctx({"rules": [_rule([("clicks", "gte", 10)], window=30)]}))
    assert len(out.findings) == 6 and out.scanned_logins == []
    assert _refused(out, "report_failed", rule="u:" + RID)


def test_disabled_and_invalid_rules_keep_their_findings(api):
    off = _rule([("clicks", "gte", 10)], rid="off111", enabled=False)
    bad = dict(_rule([("clicks", "gte", 10)], rid="bad111"), window=14)
    out = pm.run(_ctx({"rules": [off, bad]}))
    assert ("login1", "u:off111") in out.unverified
    assert ("login1", "u:bad111") in out.unverified
    assert _refused(out, "rule_invalid", rule="u:bad111")


def test_progress_and_read_summary(api):
    seen = []
    accs = (ACC, DirectAccount("login2", "t", "RUB", "Y"))
    out = pm.run(_ctx(accounts=accs, progress=seen.append))
    assert seen == [{"step": "login1", "logins_done": 1, "logins_total": 2},
                    {"step": "login2", "logins_done": 2, "logins_total": 2}]
    assert out.summary["read"] == {"logins": 2, "campaigns": 4, "objects": 6}
    assert out.summary["by_rule"]["app"] == {"found": 12, "refused": 0, "over_cap": 0}


def test_list_room_shared_with_builtins(api):
    # 899 своих + 1 словаря = потолок 900: правилу места нет
    api["campaigns"] = [_camp(1, items=["x%d.ru" % i for i in range(899)])]
    api["rows"] = [_day(1, SITES[0], 10, 5.0), _day(1, "beta.ru", 10, 50.0)]
    out = pm.run(_ctx({"rules": [_rule([("clicks", "gte", 1)])]}))
    assert [(f.rule, f.object_id) for f in out.findings] == [("app", SITES[0])]
    assert _refused(out, "list_full", rule="u:" + RID, campaign_id=1)


def test_account_protect_respects_campaign_filter(api):
    # защита по логину с only=[1]: кампания 2 вне защиты, словарь её закрывает
    protect = _rule([("name", "equals", SITES[0])], rid="ppp111", action="protect",
                    where="account", only=[1])
    out = pm.run(_ctx({"rules": [protect]}))
    assert sorted(f.campaign_id for f in out.findings if f.object_id == SITES[0]) == [2]


def test_spam_constants_match_settings_defaults():
    d = settings_for("placements", None)["spam"]
    assert (pm.spam.MIN_LEADS, pm.spam.MIN_CR, pm.spam.CR_RATIO) == (
        d["min_leads"], d["min_cr"], d["ratio"])
