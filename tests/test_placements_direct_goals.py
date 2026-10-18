# -*- coding: utf-8 -*-
"""Отчёт площадок с целями за окно 7/30: тело запроса, разбор колонок целей, очередь."""

import json

import pytest

from sync.placements import direct as D

HEADER = "\t".join(["CampaignId", "Placement", "AdNetworkType", "Cost", "Clicks",
                    "Impressions", "Conversions_111_AUTO", "Conversions_222_AUTO"])
TSV = [HEADER,
       "1\tsite.ru\tAD_NETWORK\t1500.5\t30\t900\t2\t--",
       "1\tcom.game.app\tAD_NETWORK\t200\t4\t50\t\t1",
       "2\t\tAD_NETWORK\t10\t1\t5\t0\t0",          # без имени площадки
       "2\tyandex.ru\tSEARCH\t999\t99\t999\t9\t9",   # поиск — не площадка сети
       "3\tshort.ru\tAD_NETWORK"]                    # обрезанная строка


@pytest.fixture
def report(monkeypatch):
    bodies = []

    def fake(token, login, body):
        bodies.append(body)
        return list(TSV)
    monkeypatch.setattr(D, "_report_lines", fake)
    return bodies


@pytest.mark.parametrize("days,rng", [(7, "LAST_7_DAYS"), (30, "LAST_30_DAYS")])
def test_window_sets_date_range(report, days, rng):
    D.placements_window("tok", "login1", days, [222, 111, 111])
    params = report[-1]["params"]
    assert params["DateRangeType"] == rng
    assert params["Goals"] == ["111", "222"]
    assert params["AttributionModels"] == ["AUTO"]
    assert params["IncludeVAT"] == "YES" and params["ReportType"] == "CUSTOM_REPORT"
    assert "Conversions" in params["FieldNames"] and "Impressions" in params["FieldNames"]
    assert "-%dd-" % days in params["ReportName"]


def test_goal_columns_parsed(report):
    rows = D.placements_window("tok", "login1", 30, [111, 222])
    assert rows == [
        {"campaign_id": "1", "placement": "site.ru", "clicks": 30, "cost": 1500.5,
         "impressions": 900, "conversions": {111: 2, 222: 0}},
        {"campaign_id": "1", "placement": "com.game.app", "clicks": 4, "cost": 200.0,
         "impressions": 50, "conversions": {111: 0, 222: 1}},
    ]


def test_report_name_differs_by_window_and_goals(report):
    D.placements_window("tok", "login1", 7, [111])
    D.placements_window("tok", "login1", 7, [222])
    D.placements_window("tok", "login1", 30, [111])
    names = [b["params"]["ReportName"] for b in report]
    assert len({n.rsplit("-", 1)[0] for n in names}) == 3


def test_no_goals_no_report(report):
    assert D.placements_window("tok", "login1", 7, []) == []
    assert report == []


def test_bad_window_and_goal_overflow_rejected(report):
    with pytest.raises(ValueError):
        D.placements_window("tok", "login1", 14, [111])
    with pytest.raises(ValueError):
        D.placements_window("tok", "login1", 7, list(range(1, 12)))
    assert report == []


def test_spam_wrapper_is_week_window_and_caps_goals(report):
    rows = D.placements_with_goals("tok", "login1", list(range(1, 15)))
    params = report[-1]["params"]
    assert params["DateRangeType"] == "LAST_7_DAYS"
    assert params["Goals"] == [str(g) for g in range(1, 11)]
    assert len(rows) == 2


class _Resp:
    def __init__(self, status, text=""):
        self.status, self._text = status, text

    def read(self):
        return self._text.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_queue_is_polled_until_ready(monkeypatch):
    answers = [_Resp(201), _Resp(202), _Resp(200, "\n".join(TSV) + "\n")]
    sent = []

    def urlopen(req, timeout):
        sent.append(json.loads(req.data.decode("utf-8")))
        return answers.pop(0)
    monkeypatch.setattr(D.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(D.time, "sleep", lambda s: None)
    rows = D.placements_window("tok", "login1", 30, [111, 222])
    assert len(sent) == 3 and all(b == sent[0] for b in sent)
    assert sent[0]["params"]["DateRangeType"] == "LAST_30_DAYS"
    assert [r["placement"] for r in rows] == ["site.ru", "com.game.app"]
