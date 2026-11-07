# -*- coding: utf-8 -*-
"""Статистика запросов по дням: дни загрузки, разбор отчёта, конверсии, сбои кабинетов."""

from datetime import date, datetime, timezone

from sync.modules import query_stats as qs
from sync.modules import queries as qm
from sync.modules.accounts import DirectAccount
from sync.modules.defaults import settings_for
from sync.modules.types import Context

NOW = datetime(2026, 11, 8, 9, 0, tzinfo=timezone.utc)
YDAY = date(2026, 11, 7)
GOAL = 111


def _camp(cid, goal=GOAL, ctype="TEXT_CAMPAIGN"):
    search = {"BiddingStrategyType": "HIGHEST_POSITION"}
    if goal:
        search = {"BiddingStrategyType": "WB_MAXIMUM_CONVERSION_RATE",
                  "WbMaximumConversionRate": {"GoalId": goal}}
    return {"Id": cid, "Name": "К%d" % cid, "Type": ctype, "State": "ON",
            "TextCampaign": {"BiddingStrategy": {"Search": search}}}


def _rec(day, cid, query, clicks, cost, conv=None, goal=GOAL):
    rec = {"Date": day, "CampaignId": str(cid), "Query": query,
           "Impressions": str(clicks * 10), "Clicks": str(clicks), "Cost": str(cost)}
    if conv is not None:
        rec["Conversions_%d_AUTO" % goal] = str(conv) if conv else "--"
    return rec


class Store:
    def __init__(self, last=None):
        self.last = last or {}
        self.saved = {}
        self.pruned = None

    def query_stats_last_day(self, slug, login):
        return self.last.get(login)

    def replace_query_stats(self, slug, login, start, end, rows):
        self.saved[login] = (start, end, rows)

    def prune_query_stats(self, slug, keep_from):
        self.pruned = keep_from
        return {"rows": 0, "texts": 0}


def _ctx(accounts, settings=None):
    return Context(slug="lime", module_key="queries", mode="report",
                   settings=settings_for("queries", settings), accounts=accounts,
                   now=NOW, log=lambda m: None)


def test_span_first_load_is_90_days():
    assert qs.span(None, YDAY) == (date(2026, 8, 10), YDAY)


def test_span_reloads_last_10_days_even_when_fresh():
    assert qs.span(YDAY, YDAY) == (date(2026, 10, 29), YDAY)


def test_span_continues_after_gap_but_not_beyond_90_days():
    assert qs.span(date(2026, 10, 1), YDAY) == (date(2026, 10, 2), YDAY)
    assert qs.span(date(2025, 1, 1), YDAY) == (date(2026, 8, 10), YDAY)


def test_rows_max_over_campaign_goals_and_none_without_goals():
    raw = [_rec("2026-11-01", 1, "lime платье", 3, 120.5, 2),
           _rec("2026-11-01", 2, "платье", 1, 40, 0)]
    got = qs.rows(raw, {"1": [GOAL]})
    assert ("2026-11-01", 1, "lime платье", 30, 3, 120.5, 2.0) in got
    # у кампании 2 целей нет: не ноль, а «не знаем»
    assert ("2026-11-01", 2, "платье", 10, 1, 40.0, None) in got


def test_rows_zero_conversions_stay_zero_not_none():
    got = qs.rows([_rec("2026-11-01", 1, "q", 1, 10, 0)], {"1": [GOAL]})
    assert got[0][6] == 0.0


def test_rows_aggregate_duplicates_and_skip_broken():
    raw = [_rec("2026-11-01", 1, "q", 1, 10, 1), _rec("2026-11-01", 1, "q", 2, 5, 0),
           _rec("", 1, "q", 1, 1, 0), _rec("2026-11-01", 1, " ", 1, 1, 0)]
    assert qs.rows(raw, {"1": [GOAL]}) == [("2026-11-01", 1, "q", 30, 3, 15.0, 1.0)]


def test_prepare_loads_with_strategy_goals_and_prunes(monkeypatch):
    acc = DirectAccount("lime1", "tok", "RUB", "X")
    seen = {}
    monkeypatch.setattr(qm, "_campaigns", lambda a: [_camp(1), _camp(9, ctype="CPM_BANNER")])

    def report(token, login, params, *, prefix, **kw):
        seen.update(params)
        return [_rec("2026-11-07", 1, "лайм", 2, 50, 1)]
    monkeypatch.setattr(qs.direct_api, "report_rows", report)
    store = Store()
    out = qs.prepare(_ctx([acc]), store)
    assert seen["Goals"] == [str(GOAL)]
    assert seen["FieldNames"][0] == "Date"
    # медийная кампания в отчёт не идёт
    assert seen["SelectionCriteria"]["Filter"][1]["Values"] == ["1"]
    start, end, rows = store.saved["lime1"]
    assert (start, end) == (date(2026, 8, 10), YDAY)
    assert rows == [("2026-11-07", 1, "лайм", 20, 2, 50.0, 1.0)]
    assert out["logins"]["lime1"]["rows"] == 1
    assert store.pruned == date(2026, 5, 12)


def test_prepare_lead_goals_win_over_strategy(monkeypatch):
    acc = DirectAccount("lime1", "tok", "RUB", "X")
    seen = {}
    monkeypatch.setattr(qm, "_campaigns", lambda a: [_camp(1)])
    monkeypatch.setattr(qs.direct_api, "report_rows",
                        lambda t, l, p, **kw: seen.update(p) or [])
    qs.prepare(_ctx([acc], {"lead_goal_ids": [222]}), Store())
    assert seen["Goals"] == ["222"]


def test_prepare_blind_report_gives_unknown_conversions(monkeypatch):
    acc = DirectAccount("lime1", "tok", "RUB", "X")
    monkeypatch.setattr(qm, "_campaigns", lambda a: [_camp(1)])
    monkeypatch.setattr(qs.direct_api, "report_rows",
                        lambda *a, **kw: [_rec("2026-11-07", 1, "q", 1, 10)])
    store = Store()
    qs.prepare(_ctx([acc]), store)
    assert store.saved["lime1"][2][0][6] is None


def test_prepare_one_login_failure_does_not_stop_others(monkeypatch):
    bad, good = DirectAccount("bad", "t", "RUB", "X"), DirectAccount("good", "t", "RUB", "X")

    def camps(a):
        if a.login == "bad":
            raise RuntimeError("5005")
        return [_camp(1)]
    monkeypatch.setattr(qm, "_campaigns", camps)
    monkeypatch.setattr(qs.direct_api, "report_rows", lambda *a, **kw: [])
    store = Store()
    out = qs.prepare(_ctx([bad, good]), store)
    assert "good" in store.saved and "bad" not in store.saved
    assert out["errors"][0].startswith("bad: RuntimeError")


def test_prepare_skips_login_without_search_campaigns(monkeypatch):
    acc = DirectAccount("cpm", "t", "RUB", "X")
    monkeypatch.setattr(qm, "_campaigns", lambda a: [_camp(1, ctype="CPM_BANNER")])
    store = Store()
    out = qs.prepare(_ctx([acc]), store)
    assert out["logins"] == {} and store.saved == {}
