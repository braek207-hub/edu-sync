# -*- coding: utf-8 -*-
import pytest

import sync.agent.writer.client as client_module
from sync.agent.writer.client import WriteClient, DirectWriteError, parse_units


class _FakeResponse:
    """Минимальная замена requests.Response для тестов без сети."""

    def __init__(self, status_code, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text
        self.headers = {}
        self.encoding = None

    def json(self):
        if self._json_body is None:
            raise ValueError("нет тела")
        return self._json_body


def test_sandbox_is_default():
    # Песочница по умолчанию: боевой кабинет требует явного решения.
    client = WriteClient("login-1")
    assert client.sandbox is True
    assert "api-sandbox" in client.base


def test_prod_base_when_sandbox_off():
    client = WriteClient("login-1", sandbox=False)
    assert client.base == "https://api.direct.yandex.com/json/v5"


def test_dry_run_is_default_and_blocks_mutation():
    client = WriteClient("login-1")
    assert client.dry_run is True
    assert client.is_write_allowed() is False


def test_write_allowed_only_with_explicit_apply():
    client = WriteClient("login-1", dry_run=False)
    assert client.is_write_allowed() is True


def test_parse_units_reads_remaining():
    # Заголовок Units: «израсходовано/осталось/суточный лимит».
    assert parse_units("10/4990/5000") == 4990


def test_parse_units_tolerates_garbage():
    assert parse_units("") is None
    assert parse_units("нечисло") is None


def test_error_keeps_service_and_code():
    err = DirectWriteError("bidmodifiers", 8000, "Некорректный запрос", "деталь")
    assert err.service == "bidmodifiers"
    assert err.code == 8000
    assert "деталь" in str(err)


def test_retryable_status_on_last_attempt_raises_not_empty_result(monkeypatch):
    # 500 на КАЖДОЙ попытке, включая последнюю: тело — валидный JSON без "error"
    # ({}), но статус остаётся retryable. Раньше код на последней попытке
    # проваливался в обычный разбор и возвращал {} как будто успех — журнал
    # действий пометил бы мутацию applied, хотя Директ её не применил.
    calls = []

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append(url)
        return _FakeResponse(500, json_body={}, text="upstream unavailable")

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    monkeypatch.setattr(client_module.time, "sleep", lambda seconds: None)

    client = WriteClient("login-1", dry_run=False, token="t")
    with pytest.raises(DirectWriteError):
        client.mutate("bidmodifiers", "add", {"x": 1})

    assert len(calls) == 4  # retries по умолчанию, ни одна попытка не "успешна"


def test_retryable_status_error_carries_status_code(monkeypatch):
    def fake_post(url, data=None, headers=None, timeout=None):
        return _FakeResponse(503, json_body={}, text="service unavailable")

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    monkeypatch.setattr(client_module.time, "sleep", lambda seconds: None)

    client = WriteClient("login-1", dry_run=False, token="t")
    with pytest.raises(DirectWriteError) as excinfo:
        client.mutate("bidmodifiers", "add", {"x": 1})

    assert excinfo.value.code == 503
