# -*- coding: utf-8 -*-
import json

import pytest

from sync.modules import direct_api as d
from sync.placements.direct import DirectError

PARAMS = {"FieldNames": ["Query"], "ReportType": "SEARCH_QUERY_PERFORMANCE_REPORT"}


def fake_time():
    """Часы, которые идут только от sleep."""
    state = {"t": 0.0}

    class Sleeps(list):
        def append(self, s):
            super().append(s)
            state["t"] += s
    return Sleeps(), lambda: state["t"]


def scripted(responses):
    """post, отдающий ответы по очереди; запоминает тела запросов."""
    sent = []
    queue = list(responses)

    def post(url, body, headers):
        sent.append(json.loads(body.decode("utf-8")))
        return queue.pop(0)
    post.sent = sent
    return post


def test_report_retries_same_name_on_500():
    post = scripted([(500, {}, ""), (500, {}, ""), (200, {}, "A\tB\n1\t2\n")])
    sleeps = []
    rows = d.report_rows("t", "l", dict(PARAMS), prefix="p", sleep=sleeps.append, post=post)
    names = {s["params"]["ReportName"] for s in post.sent}
    assert len(post.sent) == 3 and len(names) == 1
    assert rows == [{"A": "1", "B": "2"}]
    assert all(s >= 11 for s in sleeps)


def test_report_waits_retry_in():
    post = scripted([(201, {"retryIn": "3"}, ""), (200, {}, "A\n1\n")])
    sleeps = []
    d.report_rows("t", "l", dict(PARAMS), prefix="p", sleep=sleeps.append, post=post)
    assert sleeps == [11]


def test_report_retries_on_error_56():
    body = json.dumps({"error": {"error_code": 56}})
    post = scripted([(400, {}, body), (200, {}, "A\n1\n")])
    sleeps = []
    d.report_rows("t", "l", dict(PARAMS), prefix="p", sleep=sleeps.append, post=post)
    assert len(post.sent) == 2 and sleeps == [11]


def test_report_gives_up_after_600s():
    def post(url, body, headers):
        return 202, {"retryIn": "60"}, ""
    sleeps, clock = fake_time()
    with pytest.raises(DirectError, match="не готов за 600 с"):
        d.report_rows("t", "l", dict(PARAMS), prefix="p", sleep=sleeps.append,
                      post=post, clock=clock)
    assert sum(sleeps) <= 600


def test_huge_retry_in_capped_by_budget():
    def post(url, body, headers):
        return 201, {"retryIn": "3000"}, ""
    sleeps, clock = fake_time()
    with pytest.raises(DirectError, match="не готов за 600 с"):
        d.report_rows("t", "l", dict(PARAMS), prefix="p", sleep=sleeps.append,
                      post=post, clock=clock)
    assert sleeps == [600]


def test_network_error_retried_same_name():
    import urllib.error
    sent = []

    def post(url, body, headers):
        sent.append(json.loads(body.decode("utf-8"))["params"]["ReportName"])
        if len(sent) == 1:
            raise urllib.error.URLError("reset")
        return 200, {}, "A\n1\n"

    sleeps, clock = fake_time()
    rows = d.report_rows("t", "l", dict(PARAMS), prefix="p", sleep=sleeps.append,
                         post=post, clock=clock)
    assert rows == [{"A": "1"}] and len(set(sent)) == 1 and sleeps == [11]


def test_network_error_budget_carries_last_error():
    def post(url, body, headers):
        raise TimeoutError("timed out")
    sleeps, clock = fake_time()
    with pytest.raises(DirectError, match="timed out"):
        d.report_rows("t", "l", dict(PARAMS), prefix="p", sleep=sleeps.append,
                      post=post, clock=clock)


def test_report_name_changes_with_params():
    a = scripted([(200, {}, "A\n")])
    b = scripted([(200, {}, "A\n")])
    d.report_rows("t", "l", dict(PARAMS), prefix="p", sleep=lambda s: None, post=a)
    d.report_rows("t", "l", dict(PARAMS, DateRangeType="LAST_7_DAYS"),
                  prefix="p", sleep=lambda s: None, post=b)
    assert a.sent[0]["params"]["ReportName"] != b.sent[0]["params"]["ReportName"]
    assert a.sent[0]["params"]["ReportName"].startswith("p-")


def test_report_other_http_error_raises():
    post = scripted([(400, {}, "{}")])
    with pytest.raises(DirectError):
        d.report_rows("t", "l", dict(PARAMS), prefix="p", sleep=lambda s: None, post=post)


def test_tsv_parsed_by_header():
    post = scripted([(200, {}, "Query\tClicks\nкупить\t5\nещё\t7\n\n")])
    rows = d.report_rows("t", "l", dict(PARAMS), prefix="p", sleep=lambda s: None, post=post)
    assert rows == [{"Query": "купить", "Clicks": "5"}, {"Query": "ещё", "Clicks": "7"}]


def test_get_all_chunks_campaign_ids():
    calls = []

    def fake(token, login, service, payload):
        calls.append(payload["params"]["SelectionCriteria"]["CampaignIds"])
        return {"Keywords": [{"Id": len(calls)}]}
    out = d.get_all("t", "l", "keywords", {}, ["Id"], chunk_key="CampaignIds",
                    ids=list(range(25)), call_fn=fake)
    assert [len(c) for c in calls] == [10, 10, 5]
    assert len(out) == 3


def test_get_all_pages_by_limited_by():
    pages = [{"Campaigns": [{"Id": 1}], "LimitedBy": 1}, {"Campaigns": [{"Id": 2}]}]
    offsets = []

    def fake(token, login, service, payload):
        offsets.append(payload["params"]["Page"]["Offset"])
        return pages.pop(0)
    out = d.get_all("t", "l", "campaigns", {}, ["Id"], call_fn=fake)
    assert offsets == [0, 1] and [c["Id"] for c in out] == [1, 2]


def test_keywords_skip_autotargeting():
    def fake(token, login, service, payload):
        crit = payload["params"]["SelectionCriteria"]
        assert "States" not in crit and "Statuses" not in crit
        return {"Keywords": [{"CampaignId": 7, "Keyword": "---autotargeting"},
                             {"CampaignId": 7, "Keyword": "купить"}]}
    out = d.keywords_by_campaign("t", "l", [7], call_fn=fake)
    assert out == {"7": ["купить"]}


def test_update_campaign_list_payload():
    seen = {}

    def fake(token, login, service, payload):
        seen.update(payload)
        return {"UpdateResults": [{"Id": 5, "Errors": []}]}
    res = d.update_campaign_list("t", "l", 5, "NegativeKeywords", ["а"], call_fn=fake)
    assert seen["params"]["Campaigns"] == [{"Id": 5, "NegativeKeywords": {"Items": ["а"]}}]
    assert res == {"Id": 5, "Errors": []}


def test_read_campaign_list():
    def fake(token, login, service, payload):
        return {"Campaigns": [{"Id": 5, "ExcludedSites": {"Items": ["x.ru"]}}]}
    assert d.read_campaign_list("t", "l", 5, "ExcludedSites", call_fn=fake) == ["x.ru"]
    assert d.read_campaign_list("t", "l", 5, "NegativeKeywords",
                                call_fn=lambda *a: {"Campaigns": [{"Id": 5}]}) == []
    assert d.read_campaign_list("t", "l", 5, "ExcludedSites",
                                call_fn=lambda *a: {"Campaigns": []}) is None


def test_get_all_uses_explicit_key():
    def fake(token, login, service, payload):
        return {"Other": [{"Id": 9}], "AdGroups": [{"Id": 1}]}
    assert d.get_all("t", "l", "adgroups", {}, ["Id"], call_fn=fake) == [{"Id": 1}]
    assert d.get_all("t", "l", "x", {}, ["Id"], result_key="Other", call_fn=fake) == [{"Id": 9}]


def test_get_all_limited_by_empty_page_raises():
    def fake(token, login, service, payload):
        return {"Campaigns": [], "LimitedBy": 5}
    with pytest.raises(DirectError):
        d.get_all("t", "l", "campaigns", {}, ["Id"], call_fn=fake)
