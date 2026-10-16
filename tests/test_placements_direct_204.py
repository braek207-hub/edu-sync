# -*- coding: utf-8 -*-
"""Чистильщик площадок: 204 с пустым телом от Директа — понятная DirectError, не JSONDecodeError.

12–16.10.2026 каждый такт на четырёх кабинетах EDU падал «Expecting value: line 1 column 1»
из json.loads в call(): по логу не было видно, что не ответил Директ.
"""
import io
import json

import pytest

from sync.placements import direct as D


class _Resp(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _call_with(monkeypatch, status, body):
    monkeypatch.setattr(D.urllib.request, "urlopen", lambda req, timeout: _Resp(status, body))
    return D.call("t", "account1", "campaigns", {"method": "get", "params": {}})


@pytest.mark.parametrize("status,body", [(204, b""), (200, b"  \n")])
def test_empty_answer_is_a_readable_direct_error(monkeypatch, status, body):
    with pytest.raises(D.DirectError, match=r"campaigns: Директ API %d \(недоступен\)" % status):
        _call_with(monkeypatch, status, body)


def test_normal_answer_still_parsed(monkeypatch):
    body = json.dumps({"result": {"Campaigns": [{"Id": 1}]}}).encode("utf-8")
    assert _call_with(monkeypatch, 200, body) == {"Campaigns": [{"Id": 1}]}


def test_api_error_still_raised_with_code(monkeypatch):
    body = json.dumps({"error": {"error_code": 54, "error_string": "Нет прав"}},
                      ensure_ascii=False).encode("utf-8")
    with pytest.raises(D.DirectError, match=r"\[54\] Нет прав"):
        _call_with(monkeypatch, 200, body)
