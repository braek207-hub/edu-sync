"""Сессия синков Директа: таймаут и повтор только на временных сбоях."""

from unittest import mock

from sync import lime_direct
from sync.http_retry import DEFAULT_TIMEOUT, read_only_session


def _retry(session):
    return session.get_adapter("https://api.direct.yandex.com").max_retries


def test_retries_transient_statuses_not_report_polling():
    retry = _retry(read_only_session())
    assert retry.total and retry.connect and retry.read
    assert retry.backoff_factor > 0
    assert {429, 500, 502, 503, 504} <= set(retry.status_forcelist)
    # 201/202/204 — «отчёт готовится», 400 — код 56: их ждёт цикл опроса, не urllib3.
    assert not {200, 201, 202, 204, 400} & set(retry.status_forcelist)
    assert "POST" in retry.allowed_methods
    assert retry.raise_on_status is False


def test_default_timeout_applied_and_overridable():
    session = read_only_session()
    with mock.patch("requests.Session.request") as base:
        session.post("https://example.invalid", data=b"")
        assert base.call_args.kwargs["timeout"] == DEFAULT_TIMEOUT
        session.post("https://example.invalid", data=b"", timeout=5)
        assert base.call_args.kwargs["timeout"] == 5


def test_lime_direct_uses_retrying_session():
    assert _retry(lime_direct._HTTP).total
    assert lime_direct._HTTP.timeout == DEFAULT_TIMEOUT
