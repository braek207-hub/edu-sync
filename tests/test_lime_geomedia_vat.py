"""Геомедийка: «Потрачено» кабинета без НДС → расход с НДС по ставке дня."""

from datetime import date

from sync import lime_geomedia as geo


def _camps(day):
    return [{"campaignId": 1, "campaignName": "Билборды", "productName": "Билборд",
             "details": {"shows": "10", "clicks": "1", "spent": "1000"}}]


def test_spent_gets_vat_of_its_own_day(monkeypatch):
    monkeypatch.setattr(geo, "_VAT_OVERRIDE", "")
    monkeypatch.setattr(geo, "fetch_day", lambda page, day: _camps(day))
    rows, failed = geo.build_rows(None, date(2025, 12, 31), date(2026, 1, 1))
    assert failed == 0
    assert [r["cost"] for r in rows] == [1200.0, 1220.0]


def test_env_override_wins(monkeypatch):
    monkeypatch.setattr(geo, "_VAT_OVERRIDE", "1.0")
    assert geo.vat_mult(date(2026, 10, 1)) == 1.0
