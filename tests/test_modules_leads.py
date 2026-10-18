# -*- coding: utf-8 -*-
"""Цели-лиды и строки правил: цепочка целей, сумма лидов, невидимая цель, валюта, уровень."""

import pytest

from sync.modules import leads, queries, rules
from sync.modules.reasons import REASON_CODES
from sync.placements.classify import normalize

G1, G2 = 111, 222


def _r(cid, site, clicks, cost, conv=None, impressions=None):
    return {"campaign_id": str(cid), "placement": site, "clicks": clicks, "cost": cost,
            "impressions": clicks * 10 if impressions is None else impressions,
            "conversions": conv or {}}


def _build(raw, goals=(G1, G2), by="campaign", currency="RUB"):
    return leads.build_rows(raw, goals=list(goals), by=by, currency=currency,
                            normalize=normalize)


def _by(rows):
    return {(r.campaign_id, r.object): r for r in rows}


# --- цепочка целей ---------------------------------------------------------------

def test_module_goals_beat_project_goals():
    assert leads.lead_goals({"lead_goal_ids": [G2, "111", G2]}, [999]) == ([G1, G2], None)


def test_project_goals_when_module_has_none():
    assert leads.lead_goals({"lead_goal_ids": []}, [G2, G1]) == ([G1, G2], None)


def test_no_goals_anywhere():
    assert leads.lead_goals({"lead_goal_ids": []}, []) == ([], "no_lead_goals")
    assert leads.lead_goals({}, []) == ([], "no_lead_goals")


def test_invalid_ids_dropped_before_chain():
    # Мусор вместо целей модуля — то же, что их нет: идём к целям проекта.
    assert leads.lead_goals({"lead_goal_ids": [True, "x", 0, -5]}, [G1]) == ([G1], None)
    # isdigit() пропустил бы «²», а int() на нём падает
    assert leads.lead_goals({"lead_goal_ids": ["²"]}, [G1]) == ([G1], None)


def test_eleven_goals_refused_not_truncated():
    assert leads.lead_goals({"lead_goal_ids": list(range(1, 12))}, [G1]) == ([], "too_many_goals")
    assert leads.lead_goals({}, list(range(1, 12))) == ([], "too_many_goals")
    assert leads.lead_goals({}, list(range(1, 11)))[1] is None


def test_reason_codes_are_registered():
    assert {"no_lead_goals", "too_many_goals", "goal_not_visible"} <= set(REASON_CODES)


# --- строки ----------------------------------------------------------------------

def test_leads_sum_over_lead_goals_only():
    rows, invisible = _build([_r(1, "a.ru", 50, 1000.0, {G1: 2, G2: 3, 333: 7})])
    row = _by(rows)[(1, "a.ru")]
    assert invisible == {}
    assert row.leads_known and row.money_known and row.scope == "campaign"
    assert row.metrics == {"clicks": 50, "cost": 1000.0, "impressions": 500, "ctr": 10.0,
                           "leads": 5, "cr": 10.0, "cpl": 200.0}
    assert row.account == {"cr": 10.0, "cpl": 200.0}
    assert row.category is None


def test_string_goal_keys_after_json():
    rows, _ = _build([_r(1, "a.ru", 10, 100.0, {"111": 1, "222": 1})])
    assert rows[0].metrics["leads"] == 2


def test_campaign_with_cost_and_zero_leads_is_invisible():
    raw = [_r(1, "a.ru", 50, 1000.0, {G1: 5}),
           _r(2, "a.ru", 30, 600.0), _r(2, "b.ru", 20, 400.0, {G1: 0})]
    rows, invisible = _build(raw)
    assert invisible == {2: "goal_not_visible"}
    by = _by(rows)
    assert by[(1, "a.ru")].leads_known is True
    for key in ((2, "a.ru"), (2, "b.ru")):
        r = by[key]
        assert r.leads_known is False
        assert r.metrics["leads"] is None and r.metrics["cr"] is None and r.metrics["cpl"] is None
        # Клики и расход известны и без целей.
        assert r.metrics["clicks"] > 0 and r.metrics["cost"] > 0
    # База кабинета — только кампании, где цель видна.
    assert by[(1, "a.ru")].account == {"cr": 10.0, "cpl": 200.0}


def test_invisible_campaign_lead_conditions_unverified_in_engine():
    rows, _ = _build([_r(1, "a.ru", 50, 1000.0, {G1: 5}), _r(2, "a.ru", 30, 600.0)])
    rule_list, notes = rules.compile_rules("placements", [{
        "id": "abc123", "name": "Без лидов", "action": "propose", "window": 7,
        "conditions": [{"field": "leads", "op": "eq", "value": 0}]}])
    assert notes == []
    m = rules.evaluate(rule_list, _by(rows)[(2, "a.ru")])
    assert m.rule is None and m.unverified == ["abc123"]


def test_campaign_without_cost_is_not_invisible():
    rows, invisible = _build([_r(1, "a.ru", 50, 1000.0, {G1: 5}), _r(3, "free.ru", 4, 0.0)])
    assert invisible == {}
    row = _by(rows)[(3, "free.ru")]
    assert row.leads_known is True and row.metrics["leads"] == 0
    assert row.metrics["cr"] == 0.0 and row.metrics["cpl"] is None


def test_no_goals_means_leads_unknown_everywhere():
    rows, invisible = _build([_r(1, "a.ru", 50, 1000.0, {G1: 5})], goals=())
    assert invisible == {}
    assert rows[0].leads_known is False and rows[0].metrics["leads"] is None
    assert rows[0].account == {"cr": None, "cpl": None}


def test_kzt_money_unknown():
    rows, _ = _build([_r(1, "a.ru", 50, 1000.0, {G1: 5})], currency="KZT")
    assert rows[0].money_known is False and rows[0].leads_known is True
    rows, _ = _build([_r(1, "a.ru", 50, 1000.0, {G1: 5})], currency="rub")
    assert rows[0].money_known is True


def test_zero_bases_give_none():
    rows, _ = _build([_r(1, "a.ru", 0, 0.0, {G1: 0}, impressions=0)])
    m = rows[0].metrics
    assert m["ctr"] is None and m["cr"] is None and m["cpl"] is None


def test_account_level_sums_campaigns_of_login():
    raw = [_r(1, "Site.ru/", 50, 1000.0, {G1: 5}), _r(2, "site.ru", 30, 500.0, {G2: 1}),
           _r(2, "other.ru", 20, 100.0, {G1: 1})]
    rows, invisible = _build(raw, by="account")
    assert invisible == {}
    by = _by(rows)
    row = by[(leads.ACCOUNT_CAMPAIGN_ID, "site.ru")]
    assert row.scope == "account" and row.campaign_id == 0
    assert row.metrics["clicks"] == 80 and row.metrics["cost"] == 1500.0
    assert row.metrics["leads"] == 6 and row.metrics["impressions"] == 800
    assert row.metrics["cpl"] == 250.0 and row.metrics["cr"] == 7.5
    assert row.account == {"cr": 7.0, "cpl": 1600.0 / 7}
    assert set(by) == {(0, "site.ru"), (0, "other.ru")}


def test_account_row_touched_by_invisible_campaign_is_unknown():
    raw = [_r(1, "a.ru", 50, 1000.0, {G1: 5}), _r(2, "a.ru", 30, 600.0),
           _r(1, "b.ru", 10, 100.0, {G1: 1})]
    rows, invisible = _build(raw, by="account")
    assert invisible == {2: "goal_not_visible"}
    by = _by(rows)
    assert by[(0, "a.ru")].leads_known is False
    assert by[(0, "b.ru")].leads_known is True


def test_rows_ordered_by_cost_and_bad_rows_skipped():
    raw = [_r(1, "cheap.ru", 5, 10.0, {G1: 1}), _r(1, "dear.ru", 5, 900.0, {G1: 1}),
           _r("", "x.ru", 5, 10.0), _r(1, "", 5, 10.0)]
    rows, _ = _build(raw)
    assert [r.object for r in rows] == ["dear.ru", "cheap.ru"]


def test_unknown_by_rejected():
    with pytest.raises(ValueError):
        _build([], by="group")


def test_query_rows_share_the_same_path():
    report = [{"CampaignId": "7", "Query": "купить диплом", "Impressions": "400",
               "Clicks": "40", "Cost": "4000", "Conversions_111_AUTO": "2",
               "Conversions_222_AUTO": "--"},
              {"CampaignId": "7", "Query": "заочно", "Impressions": "100", "Clicks": "10",
               "Cost": "500", "Conversions_111_AUTO": "--", "Conversions_222_AUTO": "1"}]
    q = queries._query_rows(report, {"7": [G1]})
    rows, invisible = leads.build_rows(queries.rule_raw(q), goals=[G1, G2], by="campaign",
                                       currency="RUB", normalize=str.strip, key="query")
    by = _by(rows)
    diploma = by[(7, "купить диплом")]
    assert diploma.metrics["impressions"] == 400 and diploma.metrics["ctr"] == 10.0
    assert diploma.metrics["leads"] == 2 and diploma.metrics["cpl"] == 2000.0
    # Лид по цели 222 виден правилам, хотя цель стратегии кампании — только 111.
    assert by[(7, "заочно")].metrics["leads"] == 1
    assert invisible == {}
