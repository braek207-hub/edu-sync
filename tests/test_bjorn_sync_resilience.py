"""Синк BJORN переживает три ответа API, на которых падал в октябре 2026.

02.10 и 07.10 — stat API Метрики «Запрос слишком сложный» на 7-дневном окне;
11.10 — Reports API Директа отдал 204 с пустым телом; 04.10 — Logs API не успел.
"""

import json
from datetime import date, timedelta

import pytest

from sync.bjorn import common, sync_card_views, sync_direct

TOO_COMPLEX_RU = json.dumps(
    {
        "errors": [{"error_type": "query_error", "message": "Запрос слишком сложный. Пожалуйста, уменьшите интервал дат или семплирование."}],
        "code": 400,
    },
    ensure_ascii=False,
)
TOO_COMPLEX_EN = '{"errors":[{"error_type":"query_error","message":"Query is too complicated. Please reduce the date interval or sampling."}],"code":400}'


class Resp:
    def __init__(self, status, body="", headers=None):
        self.status_code = status
        self.text = body if isinstance(body, str) else json.dumps(body)
        self.headers = headers or {}
        self.encoding = None

    def json(self):
        return json.loads(self.text)


def days(d1, d2):
    start, end = date.fromisoformat(d1), date.fromisoformat(d2)
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(common.time, "sleep", lambda _s: None)
    monkeypatch.setattr(sync_direct.time, "sleep", lambda _s: None)
    monkeypatch.setattr(sync_card_views.time, "sleep", lambda _s: None)


def fake_metrica(max_days, body=TOO_COMPLEX_RU):
    """Метрика, которая отказывает на окне длиннее max_days и отдаёт по строке на день."""
    calls = []

    def get(url, params=None, headers=None, timeout=None):
        calls.append(dict(params))
        span = days(params["date1"], params["date2"])
        if len(span) > max_days:
            return Resp(400, body)
        return Resp(200, {"data": [{"dimensions": [{"name": d}], "metrics": [1]} for d in span]})

    return get, calls


PARAMS = {"ids": "1", "metrics": "ym:s:visits", "dimensions": "ym:s:date,ym:s:lastsignTrafficSource"}


def test_too_complex_window_is_halved_until_it_passes(monkeypatch):
    get, calls = fake_metrica(max_days=2)
    monkeypatch.setattr(common.requests, "get", get)
    rows = common.metrica_stat_rows({**PARAMS, "date1": "2026-10-04", "date2": "2026-10-10"}, "t")
    got = [r["dimensions"][0]["name"] for r in rows]
    assert got == days("2026-10-04", "2026-10-10")  # каждый день ровно один раз, по порядку
    assert len(calls) > 1


def test_english_message_is_recognised_too(monkeypatch):
    get, _ = fake_metrica(max_days=3, body=TOO_COMPLEX_EN)
    monkeypatch.setattr(common.requests, "get", get)
    rows = common.metrica_stat_rows({**PARAMS, "date1": "2026-10-01", "date2": "2026-10-07"}, "t")
    assert len(rows) == 7


def test_single_day_still_too_complex_raises(monkeypatch):
    get, _ = fake_metrica(max_days=0)
    monkeypatch.setattr(common.requests, "get", get)
    with pytest.raises(common.MetricaQueryTooComplex):
        common.metrica_stat_rows({**PARAMS, "date1": "2026-10-01", "date2": "2026-10-03"}, "t")


def test_other_400_is_not_split(monkeypatch):
    calls = []

    def get(url, params=None, headers=None, timeout=None):
        calls.append(params)
        return Resp(400, '{"errors":[{"error_type":"invalid_parameter","message":"Wrong parameter"}]}')

    monkeypatch.setattr(common.requests, "get", get)
    with pytest.raises(RuntimeError) as err:
        common.metrica_stat_rows({**PARAMS, "date1": "2026-10-01", "date2": "2026-10-07"}, "t")
    assert not isinstance(err.value, common.MetricaQueryTooComplex)
    assert len(calls) == 1


def test_pages_fetched_before_the_error_are_not_duplicated(monkeypatch):
    # Окно проходит первой страницей и падает на второй: половины не должны повторить первую.
    calls = []

    def get(url, params=None, headers=None, timeout=None):
        calls.append(dict(params))
        span = days(params["date1"], params["date2"])
        if len(span) > 2 and params["offset"] > 1:
            return Resp(400, TOO_COMPLEX_RU)
        rows = [{"dimensions": [{"name": d}], "metrics": [1]} for d in span]
        start = params["offset"] - 1
        return Resp(200, {"data": rows[start:start + params["limit"]]})

    monkeypatch.setattr(common.requests, "get", get)
    rows = common.metrica_stat_rows(
        {**PARAMS, "date1": "2026-10-01", "date2": "2026-10-04"}, "t", page_limit=2
    )
    assert [r["dimensions"][0]["name"] for r in rows] == days("2026-10-01", "2026-10-04")


def test_retryable_status_is_retried(monkeypatch):
    answers = [Resp(429, "slow down"), Resp(200, {"data": [{"dimensions": [{"name": "2026-10-01"}]}]})]
    monkeypatch.setattr(common.requests, "get", lambda *a, **k: answers.pop(0))
    rows = common.metrica_stat_rows({**PARAMS, "date1": "2026-10-01", "date2": "2026-10-01"}, "t")
    assert len(rows) == 1


def test_split_requires_date_dimension():
    with pytest.raises(ValueError):
        common.metrica_stat_rows(
            {"ids": "1", "metrics": "ym:s:visits", "dimensions": "ym:s:lastsignTrafficSource",
             "date1": "2026-10-01", "date2": "2026-10-07"},
            "t",
        )


def test_direct_204_is_retried_not_read_as_empty(monkeypatch):
    answers = [Resp(204, ""), Resp(200, "Date\tCost\n2026-10-10\t100\n")]
    monkeypatch.setattr(sync_direct.requests, "post", lambda *a, **k: answers.pop(0))
    text = sync_direct.fetch_report("login", "2026-10-04", "2026-10-10", ["Date", "Cost"], token="t")
    assert "2026-10-10" in text


def test_direct_204_forever_fails_instead_of_returning_nothing(monkeypatch):
    monkeypatch.setattr(sync_direct.requests, "post", lambda *a, **k: Resp(204, ""))
    with pytest.raises(RuntimeError):
        sync_direct.fetch_report("login", "2026-10-04", "2026-10-10", ["Date"], token="t")


def test_logs_wait_keeps_waiting_through_awaiting_retry(monkeypatch):
    statuses = ["created", "awaiting_retry", "awaiting_retry", "processed"]
    monkeypatch.setattr(
        sync_card_views.requests,
        "get",
        lambda *a, **k: Resp(200, {"log_request": {"status": statuses.pop(0), "parts": []}}),
    )
    info = sync_card_views.logs_wait("1", {}, 7, deadline=float("inf"))
    assert info["status"] == "processed"


def test_logs_wait_stops_at_the_step_deadline(monkeypatch):
    monkeypatch.setattr(
        sync_card_views.requests,
        "get",
        lambda *a, **k: Resp(200, {"log_request": {"status": "created"}}),
    )
    with pytest.raises(RuntimeError, match="статус created"):
        sync_card_views.logs_wait("1", {}, 7, deadline=0.0)
