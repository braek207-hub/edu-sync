"""Logs API: 400 «Query is too complicated» — нагрузка Яндекса, повторяем, а не роняем синк."""
import pytest

import sync.logs_api as logs_api

TOO_COMPLEX = ('{"errors":[{"error_type":"query_error","message":"Query is too complicated. '
               'Please reduce the date interval or sampling."}],"code":400}')


class _Resp:
    def __init__(self, status, text="", body=None):
        self.status_code, self.text, self._body = status, text, body

    def json(self):
        return self._body


def _patch(monkeypatch, responses):
    calls = []

    def fake_request(method, url, **kw):
        calls.append(method)
        return responses.pop(0)

    monkeypatch.setattr(logs_api.requests, "request", fake_request)
    monkeypatch.setattr(logs_api.time, "sleep", lambda s: None)
    return calls


def test_create_request_retries_too_complicated(monkeypatch):
    calls = _patch(monkeypatch, [
        _Resp(400, TOO_COMPLEX),
        _Resp(200, body={"log_request": {"request_id": 42}}),
    ])
    assert logs_api.create_request("1", "2026-11-11", "2026-11-11", "f", "t", source="hits") == 42
    assert calls == ["POST", "POST"]


def test_other_400_still_fails_immediately(monkeypatch):
    calls = _patch(monkeypatch, [_Resp(400, '{"message":"Invalid field"}')])
    with pytest.raises(RuntimeError, match="400"):
        logs_api.create_request("1", "2026-11-11", "2026-11-11", "f", "t")
    assert calls == ["POST"]


def test_too_complicated_gives_up_after_limit(monkeypatch):
    calls = _patch(monkeypatch, [_Resp(400, TOO_COMPLEX) for _ in range(6)])
    with pytest.raises(RuntimeError, match="too complicated"):
        logs_api.create_request("1", "2026-11-11", "2026-11-11", "f", "t")
    assert len(calls) == 6
