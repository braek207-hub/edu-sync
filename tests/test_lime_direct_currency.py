# -*- coding: utf-8 -*-
"""Конверсия денежных полей KZ-кабинета: тенге → рубли (курс ЦБ на дату) × НДС.

Кабинет lime-kz1 ведётся в тенге, а lime_direct_stats — общая с рублёвым РФ-кабинетом.
Без конверсии тенге подмешивались бы к рублям (расход занижен ~в 6 раз). Reports API
в KZ отдаёт Cost без НДС (IncludeVAT=YES≡NO, проверено 2026-07-22), казахстанские 16%
добавляем сами. РФ-прогон (валюта пустая/RUB, НДС×1) конверсию не трогает.
"""
import sync.lime_direct as ld
from sync.lime_direct import _convert_money, _MONEY_FIELDS


def _row(**over):
    base = {
        "date": "2026-07-20", "cost": 100.0, "avg_effective_bid": 10.0,
        "weekly_budget": 700.0, "daily_budget": 100.0, "target_cpa": 50.0,
    }
    base.update(over)
    return base


def test_kzt_with_vat(monkeypatch):
    monkeypatch.setattr(ld, "fx_to_rub", lambda cur, d: 0.20)  # 1 тенге = 0.20 ₽
    rows = [_row()]
    _convert_money(rows, "KZT", 1.16)
    # 100 тенге × 0.20 × 1.16 = 23.2 ₽
    assert rows[0]["cost"] == 23.2
    assert rows[0]["avg_effective_bid"] == 2.32
    assert rows[0]["target_cpa"] == 11.6


def test_rub_noop_leaves_values(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(ld, "fx_to_rub", lambda cur, d: called.__setitem__("n", called["n"] + 1) or 0.2)
    rows = [_row()]
    _convert_money(rows, "", 1.0)
    assert rows[0]["cost"] == 100.0  # РФ-прогон не трогает
    assert called["n"] == 0  # ЦБ не дёргается


def test_vat_only_without_fx(monkeypatch):
    monkeypatch.setattr(ld, "fx_to_rub", lambda cur, d: (_ for _ in ()).throw(AssertionError("fx не должен вызываться")))
    rows = [_row()]
    _convert_money(rows, "RUB", 1.22)  # рублёвый источник, только НДС
    assert rows[0]["cost"] == 122.0


def test_none_fields_survive(monkeypatch):
    monkeypatch.setattr(ld, "fx_to_rub", lambda cur, d: 0.20)
    rows = [_row(weekly_budget=None, target_cpa=None)]
    _convert_money(rows, "KZT", 1.16)
    assert rows[0]["weekly_budget"] is None
    assert rows[0]["target_cpa"] is None
    assert rows[0]["cost"] == 23.2


def test_rate_cached_per_date(monkeypatch):
    calls = []
    monkeypatch.setattr(ld, "fx_to_rub", lambda cur, d: calls.append(d) or 0.20)
    rows = [_row(date="2026-07-20"), _row(date="2026-07-20"), _row(date="2026-07-21")]
    _convert_money(rows, "KZT", 1.16)
    assert calls == ["2026-07-20", "2026-07-21"]  # один запрос на дату


def test_all_money_fields_covered():
    # Страховка: если в lime_direct_stats добавят денежное поле, тест напомнит внести его.
    assert _MONEY_FIELDS == ("cost", "avg_effective_bid", "weekly_budget", "daily_budget", "target_cpa")
