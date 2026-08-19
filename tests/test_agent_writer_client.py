# -*- coding: utf-8 -*-
import pytest

from sync.agent.writer.client import WriteClient, DirectWriteError, parse_units


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
