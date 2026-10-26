# -*- coding: utf-8 -*-
"""Чистильщик площадок: очередь Reports — ошибка 56 и retryIn не роняют прогон.

21–25.10.2026 чистильщик падал по 3–4 раза в сутки на «Превышен лимит запросов
метода… не чаще чем 1 раз в 10 секунд» (лимит на кабинет общий с другими синками)
и на «отчёт не сформировался за 12 попыток» (2 минуты в часы пик мало).
"""
import io
import json
import urllib.error

import pytest

from sync.placements import direct as D


class _Resp(io.BytesIO):
    def __init__(self, status, body=b"", headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _rate_limited():
    body = json.dumps({"error": {"error_code": "56",
                                 "error_string": "Превышен лимит запросов метода"}},
                      ensure_ascii=False).encode("utf-8")
    return urllib.error.HTTPError(D.REPORTS, 400, "Bad Request", {}, io.BytesIO(body))


def _run(monkeypatch, answers):
    sleeps = []
    it = iter(answers)

    def urlopen(req, timeout):
        a = next(it)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(D.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(D.time, "sleep", sleeps.append)
    return D._report_lines("t", "account1", {"params": {}}), sleeps


def test_rate_limit_56_waits_and_retries(monkeypatch):
    lines, sleeps = _run(monkeypatch, [_rate_limited(), _Resp(200, b"A\tB\n1\t2\n")])
    assert lines == ["A\tB", "1\t2"]
    assert sleeps == [D.POLL_SLEEP]


def test_retry_in_header_respected_but_not_below_limit(monkeypatch):
    lines, sleeps = _run(monkeypatch, [
        _Resp(201, headers={"retryIn": "30"}),
        _Resp(202, headers={"retryIn": "2"}),
        _Resp(200, b"A\n"),
    ])
    assert lines == ["A"]
    assert sleeps == [30, D.POLL_SLEEP]


def test_other_api_error_still_raised(monkeypatch):
    body = json.dumps({"error": {"error_code": "8000", "error_string": "Неверный запрос"}},
                      ensure_ascii=False).encode("utf-8")
    err = urllib.error.HTTPError(D.REPORTS, 400, "Bad Request", {}, io.BytesIO(body))
    with pytest.raises(D.DirectError, match="8000"):
        _run(monkeypatch, [err])


def test_endless_queue_gives_up(monkeypatch):
    with pytest.raises(D.DirectError, match="не сформировался"):
        _run(monkeypatch, [_Resp(202) for _ in range(D.MAX_POLL)])
