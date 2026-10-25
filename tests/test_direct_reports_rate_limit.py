"""Reports API: ошибка 56 «не чаще раза в 10 секунд» — повтор, а не падение синка.

С ~11.10.2026 LIME, EDU и BJORN падали каждую ночь: Директ отдавал 201 с retryIn=1–2 с,
синк опрашивал через 1–2 с и получал 400/56.
"""

import json

import pytest

from sync import direct, direct_reports_limit, lime_direct
from sync.bjorn import sync_direct as bjorn_direct

LIMIT_56 = json.dumps({"error": {"request_id": "1", "error_code": "56",
                                 "error_string": "Exceeded limit of method requests",
                                 "error_detail": "This method can't be requested more than 1 times in 10 seconds"}})
OTHER_400 = json.dumps({"error": {"request_id": "2", "error_code": "8000", "error_string": "Invalid request"}})


class Resp:
    def __init__(self, status, text="", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}
        self.encoding = None


@pytest.fixture
def sleeps(monkeypatch):
    log = []
    for mod in (direct, lime_direct, bjorn_direct):
        monkeypatch.setattr(mod.time, "sleep", log.append)
    return log


def scripted(responses):
    it = iter(responses)
    return lambda *a, **k: next(it)


def test_is_rate_limited_only_on_code_56():
    assert direct_reports_limit.is_rate_limited(400, LIMIT_56)
    assert not direct_reports_limit.is_rate_limited(400, OTHER_400)
    assert not direct_reports_limit.is_rate_limited(400, "<html>bad gateway</html>")
    assert not direct_reports_limit.is_rate_limited(201, LIMIT_56)


def test_poll_delay_never_below_method_limit():
    assert direct_reports_limit.poll_delay("1", 30) == direct_reports_limit.MIN_POLL_SECONDS
    assert direct_reports_limit.poll_delay(None, 60) == 60
    assert direct_reports_limit.poll_delay("45", 5) == 45


def test_edu_direct_retries_after_56(monkeypatch, sleeps):
    monkeypatch.setenv("DIRECT_TOKEN", "t")
    monkeypatch.setattr(direct.requests, "post", scripted([
        Resp(201, headers={"retryIn": "2"}), Resp(400, LIMIT_56), Resp(200, ""),
    ]))
    assert direct._fetch_report("account10", "2026-10-10", "2026-10-16", []) == []
    assert sleeps and min(sleeps) >= direct_reports_limit.MIN_POLL_SECONDS


def test_edu_direct_other_400_still_fails(monkeypatch, sleeps):
    monkeypatch.setenv("DIRECT_TOKEN", "t")
    monkeypatch.setattr(direct.requests, "post", scripted([Resp(400, OTHER_400)]))
    with pytest.raises(RuntimeError, match="8000"):
        direct._fetch_report("account10", "2026-10-10", "2026-10-16", [])


def test_lime_direct_retries_after_56(monkeypatch, sleeps):
    monkeypatch.setattr(lime_direct, "_report_headers", lambda: {})
    monkeypatch.setattr(lime_direct._HTTP, "post", scripted([
        Resp(201, headers={"retryIn": "1"}), Resp(400, LIMIT_56), Resp(200, "CampaignId\tDate\n"),
    ]))
    assert lime_direct._fetch_report_chunk("2026-10-10", "2026-10-16", [], {}) == []
    assert min(sleeps) >= direct_reports_limit.MIN_POLL_SECONDS


def test_bjorn_direct_retries_after_56(monkeypatch, sleeps):
    monkeypatch.setattr(bjorn_direct.requests, "post", scripted([
        Resp(202, headers={"retryIn": "1"}), Resp(400, LIMIT_56), Resp(200, "ok"),
    ]))
    assert bjorn_direct.fetch_report("audit-bjornlarsen", "2026-10-10", "2026-10-16", ["Date"], token="t") == "ok"
    assert min(sleeps) >= direct_reports_limit.MIN_POLL_SECONDS


def test_bjorn_direct_gives_up_on_persistent_56(monkeypatch, sleeps):
    monkeypatch.setattr(bjorn_direct.requests, "post", lambda *a, **k: Resp(400, LIMIT_56))
    with pytest.raises(RuntimeError, match="56"):
        bjorn_direct.fetch_report("audit-bjornlarsen", "2026-10-10", "2026-10-16", ["Date"], token="t")
