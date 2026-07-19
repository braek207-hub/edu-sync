# -*- coding: utf-8 -*-
"""Разбор ответа Stat API для KZ-среза LIME."""
import pytest
import requests

from sync import lime_kz_metrika_api as api
from sync.lime_kz_metrika_api import DIMENSIONS, GEO_FILTER, parse_metrika_kz


def _resp(data):
    return {"query": {"dimensions": list(DIMENSIONS)}, "data": data}


def test_parse_maps_dimensions_and_metrics():
    rows = parse_metrika_kz(_resp([{
        "dimensions": [
            {"name": "2026-07-15"},
            {"id": "ad", "name": "Ad traffic"},
            {"name": "Google Ads"},
            {"name": None},
            {"name": "23952118304"},
            {"name": None},
        ],
        "metrics": [4617.0, 2100.0, 900.0, 18.5, 12.3, 700.0, 250.0, 79.0, 2112310.0],
    }]))

    assert rows == [{
        "date": "2026-07-15",
        "traffic_source": "ad",
        "source_engine": "Google Ads",
        "direct_campaign_name": None,
        "utm_campaign": "23952118304",
        "utm_content": None,
        "visits": 4617.0,
        "users": 2100.0,
        "new_users": 900.0,
        "bounce_rate": 18.5,
        "page_depth": 12.3,
        "cart_reaches": 700.0,
        "checkout_reaches": 250.0,
        "orders": 79.0,
        "revenue": 2112310.0,
    }]


def test_parse_survives_dimension_order_change():
    """Позиции берутся из эха запроса — перестановка измерений не ломает разбор."""
    swapped = ["ym:s:lastsignTrafficSource", "ym:s:date"] + list(DIMENSIONS[2:])
    resp = {"query": {"dimensions": swapped}, "data": [{
        "dimensions": [
            {"id": "organic", "name": "Search engine traffic"},
            {"name": "2026-07-16"},
            {"name": "Google"},
            {"name": None},
            {"name": None},
            {"name": None},
        ],
        "metrics": [10281.0, 4637.0, 3000.0, 20.0, 9.0, 500.0, 200.0, 165.0, 6244990.0],
    }]}
    row = parse_metrika_kz(resp)[0]
    assert row["date"] == "2026-07-16"
    assert row["traffic_source"] == "organic"
    assert row["source_engine"] == "Google"


def test_parse_tolerates_missing_metrics():
    """Короткий массив метрик не роняет разбор — недостающие становятся нулями."""
    row = parse_metrika_kz(_resp([{
        "dimensions": [{"name": "2026-07-15"}, {"id": "direct"}, {"name": None},
                       {"name": None}, {"name": None}, {"name": None}],
        "metrics": [12.0],
    }]))[0]
    assert row["visits"] == 12.0
    assert row["revenue"] == 0.0
    assert row["cart_reaches"] == 0.0


def test_geo_filter_targets_kazakhstan():
    assert GEO_FILTER == "ym:s:regionCountryName=='Kazakhstan'"


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code != 200:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _ok_payload():
    return {"query": {"dimensions": list(DIMENSIONS)}, "data": [{
        "dimensions": [{"name": "2026-04-05"}, {"id": "ad"}, {"name": "Google Ads"},
                       {"name": None}, {"name": None}, {"name": None}],
        "metrics": [10.0, 8.0, 4.0, 20.0, 3.0, 2.0, 1.0, 1.0, 5000.0],
    }]}


def test_fetch_retries_transient_error(monkeypatch):
    """Stat API отдаёт транзиентную 400 на отдельной дате — прогон не должен падать.

    Реальный случай 2026-07-19: 400 на 2026-04-05 уронила бэкфилл, тот же запрос минутой
    позже вернул 200. Без повтора ежедневный синк тихо теряет день.
    """
    calls = []

    def fake_get(url, **kw):
        calls.append(kw["params"]["date1"])
        return _Resp(400) if len(calls) == 1 else _Resp(200, _ok_payload())

    monkeypatch.setattr(api.requests, "get", fake_get)
    monkeypatch.setattr(api.time, "sleep", lambda s: None)

    rows = api.fetch_kz_traffic(23504302, "tok", "2026-04-05", "2026-04-05")
    assert len(calls) == 2, "второй попытки не было — ретрай не сработал"
    assert rows[0]["visits"] == 10.0


def test_fetch_raises_after_all_retries(monkeypatch):
    """Постоянная ошибка обязана дойти до вызывающего, а не притвориться пустыми данными."""
    monkeypatch.setattr(api.requests, "get", lambda url, **kw: _Resp(403))
    monkeypatch.setattr(api.time, "sleep", lambda s: None)

    with pytest.raises(requests.HTTPError):
        api.fetch_kz_traffic(23504302, "tok", "2026-04-05", "2026-04-05")
