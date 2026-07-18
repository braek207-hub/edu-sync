# -*- coding: utf-8 -*-
"""Устойчивость конвертации валют к недоступности ЦБ.

Боевой случай 2026-07-18: бэкфилл cost_rub дёрнул курс на сотни дат подряд, cbr.ru
ответил ConnectTimeout на одной из них — и это уронило ВЕСЬ workflow вместе с шагами
после конвертации. Ретраев не было, ошибка одной даты была фатальной.
"""
import sync.fx as fx


class _FakeResp:
    def __init__(self, text):
        self.text = text
        self.encoding = None


def _clear_caches():
    fx._CACHE.clear()
    fx._XML_CACHE.clear()


def test_xml_cached_per_date_one_request_for_all_currencies(monkeypatch):
    """USD и AED на одну дату — один запрос: XML_daily отдаёт все валюты сразу."""
    _clear_caches()
    calls = []
    xml = (
        '<?xml version="1.0" encoding="windows-1251"?><ValCurs>'
        '<Valute ID="R01235"><Value>78,50</Value><Nominal>1</Nominal></Valute>'
        '<Valute ID="R01230"><Value>21,35</Value><Nominal>1</Nominal></Valute>'
        "</ValCurs>"
    )

    def fake_get(url, params=None, timeout=None):
        calls.append(params["date_req"])
        return _FakeResp(xml)

    monkeypatch.setattr(fx.requests, "get", fake_get)
    assert fx.to_rub("USD", "2026-07-16") == 78.5
    assert fx.to_rub("AED", "2026-07-16") == 21.35
    assert len(calls) == 1


def test_retries_then_succeeds(monkeypatch):
    """Временный сбой сети не должен быть фатальным."""
    _clear_caches()
    monkeypatch.setattr(fx.time, "sleep", lambda *_: None)
    attempts = {"n": 0}
    xml = (
        '<?xml version="1.0" encoding="windows-1251"?><ValCurs>'
        '<Valute ID="R01235"><Value>78,50</Value><Nominal>1</Nominal></Valute>'
        "</ValCurs>"
    )

    def flaky_get(url, params=None, timeout=None):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise fx.requests.ConnectTimeout("timeout")
        return _FakeResp(xml)

    monkeypatch.setattr(fx.requests, "get", flaky_get)
    assert fx.to_rub("USD", "2026-07-16") == 78.5
    assert attempts["n"] == 3


def test_raises_after_all_retries(monkeypatch):
    """Исчерпав попытки, поднимаем понятную ошибку — её ловит вызывающий шаг."""
    _clear_caches()
    monkeypatch.setattr(fx.time, "sleep", lambda *_: None)

    def always_fail(url, params=None, timeout=None):
        raise fx.requests.ConnectTimeout("timeout")

    monkeypatch.setattr(fx.requests, "get", always_fail)
    try:
        fx.to_rub("USD", "2026-07-16")
    except RuntimeError as e:
        assert "cbr.ru недоступен" in str(e)
    else:
        raise AssertionError("должно было подняться RuntimeError")
