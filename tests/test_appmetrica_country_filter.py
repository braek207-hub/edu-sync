"""RU-витрины приложения держат только события RU-устройств — как витрина PROCONTEXT region='ru'."""
import pytest

from sync.appmetrica_logs import app_country, only_country


def test_default_country_is_ru(monkeypatch):
    monkeypatch.delenv("APP_COUNTRY", raising=False)
    assert app_country() == "RU"


def test_empty_env_disables_filter(monkeypatch):
    monkeypatch.setenv("APP_COUNTRY", "")
    assert app_country() == ""
    rows = [{"country_iso_code": "DE"}, {}]
    assert only_country(rows) == rows


def test_only_country_keeps_ru_by_event_country(monkeypatch):
    monkeypatch.setenv("APP_COUNTRY", "ru")
    rows = [
        {"appmetrica_device_id": "a", "country_iso_code": "RU"},
        {"appmetrica_device_id": "b", "country_iso_code": "DE"},  # заказ 19 999 ×2 у 30072646
        {"appmetrica_device_id": "c", "country_iso_code": "ru"},
        {"appmetrica_device_id": "d"},  # запрос без гео — не считаем «своим» молча
    ]
    assert [r["appmetrica_device_id"] for r in only_country(rows)] == ["a", "c"]


@pytest.mark.parametrize("iso", ["KZ", "AE"])
def test_only_country_explicit_iso(iso):
    rows = [{"country_iso_code": "RU"}, {"country_iso_code": iso}]
    assert only_country(rows, iso) == [{"country_iso_code": iso}]
