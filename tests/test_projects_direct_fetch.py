"""Выгрузка Директа по логину для общего синка проектов (Фаза B, Task 4)."""

import json

import pytest

from sync.bjorn import sync_direct as bjorn_direct
from sync.projects import direct_fetch
from sync.projects.direct_fetch import DirectRow, fetch_direct_daily

HEADER = "Date\tCampaignId\tCampaignName\tImpressions\tClicks\tCost"


class Resp:
    def __init__(self, status, text="", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}
        self.encoding = None


@pytest.fixture
def sleeps(monkeypatch):
    log = []
    monkeypatch.setattr(bjorn_direct.time, "sleep", log.append)
    return log


def scripted(responses):
    it = iter(responses)
    return lambda *a, **k: next(it)


def test_max_wait_raises_timeout(monkeypatch, sleeps):
    monkeypatch.setattr(bjorn_direct.requests, "post", lambda *a, **k: Resp(201))
    with pytest.raises(TimeoutError, match="не готов за 150"):
        bjorn_direct.fetch_report("lg", "2026-10-10", "2026-10-16", ["Date"], token="t", max_wait_sec=150)
    assert sleeps and sum(sleeps) <= 150


def test_max_wait_none_unchanged(monkeypatch, sleeps):
    monkeypatch.setattr(
        bjorn_direct.requests, "post",
        scripted([Resp(201), Resp(202, headers={"retryIn": "30"}), Resp(200, "ok")]),
    )
    assert bjorn_direct.fetch_report("lg", "2026-10-10", "2026-10-16", ["Date"], token="t") == "ok"
    assert sleeps == [60, 30]


def test_max_wait_counts_server_error_backoff(monkeypatch, sleeps):
    monkeypatch.setattr(bjorn_direct.requests, "post", lambda *a, **k: Resp(503))
    with pytest.raises(TimeoutError):
        bjorn_direct.fetch_report("lg", "2026-10-10", "2026-10-16", ["Date"], token="t", max_wait_sec=25)
    assert sleeps and sum(sleeps) <= 25


def test_max_wait_counts_rate_limit_sleep(monkeypatch, sleeps):
    limit_56 = json.dumps({"error": {"error_code": "56", "error_string": "Exceeded limit of method requests"}})
    monkeypatch.setattr(bjorn_direct.requests, "post", lambda *a, **k: Resp(400, limit_56))
    with pytest.raises(TimeoutError):
        bjorn_direct.fetch_report("lg", "2026-10-10", "2026-10-16", ["Date"], token="t", max_wait_sec=25)
    assert sleeps and sum(sleeps) <= 25


def test_max_wait_202_retry_in_over_budget(monkeypatch, sleeps):
    monkeypatch.setattr(bjorn_direct.requests, "post", lambda *a, **k: Resp(202, headers={"retryIn": "200"}))
    with pytest.raises(TimeoutError):
        bjorn_direct.fetch_report("lg", "2026-10-10", "2026-10-16", ["Date"], token="t", max_wait_sec=150)
    assert sleeps == []


def test_parses_and_sums_duplicates(monkeypatch, sleeps):
    text = "\n".join([
        HEADER,
        "2026-10-10\t11\tКамп А\t100\t5\t10.004",
        "2026-10-10\t11\tКамп А\t50\t2\t20.006",
        "2026-10-09\t12\tКамп Б\t10\t1\t3.50",
    ])
    monkeypatch.setattr(bjorn_direct.requests, "post", scripted([Resp(200, text)]))
    rows = fetch_direct_daily("lg", "t", "2026-10-09", "2026-10-10", max_wait_sec=100)
    assert rows == [
        DirectRow("2026-10-09", 12, "Камп Б", 3.5, 1, 10),
        DirectRow("2026-10-10", 11, "Камп А", 30.01, 7, 150),
    ]


def test_skips_total_and_dash_rows(monkeypatch, sleeps):
    text = "\n".join([
        HEADER,
        "2026-10-10\t11\tA\t1\t1\t1.00",
        "2026-10-10\t--\tTotal\t9\t9\t9.00",
        "2026-10-10\t\tX\t9\t9\t9.00",
    ])
    monkeypatch.setattr(bjorn_direct.requests, "post", scripted([Resp(200, text)]))
    rows = fetch_direct_daily("lg", "t", "2026-10-10", "2026-10-10", max_wait_sec=100)
    assert [r.campaign_id for r in rows] == [11]


def test_request_has_vat_and_client_login(monkeypatch, sleeps):
    seen = {}

    def post(url, **kw):
        seen.update(kw)
        return Resp(200, HEADER)

    monkeypatch.setattr(bjorn_direct.requests, "post", post)
    assert direct_fetch.fetch_direct_daily("my-login", "t", "2026-10-10", "2026-10-10", max_wait_sec=10) == []
    assert json.loads(seen["data"])["params"]["IncludeVAT"] == "YES"
    assert seen["headers"]["Client-Login"] == "my-login"
