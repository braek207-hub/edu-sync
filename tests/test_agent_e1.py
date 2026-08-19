# -*- coding: utf-8 -*-
"""
tests/test_agent_e1.py — тесты двух отклонений оркестратора Э1a от исходного
плана задачи (см. task-9-report.md): кампании только своего кабинета,
нормализация демографических корректировок с двумя измерениями сразу.

Все тесты — чистые функции: WriteClient подменён фейком по протоколу .get(),
без сети и без БД.
"""

import sync.agent_e1 as agent_e1


class _FakeCampaignsClient:
    """Отдаёт кампании страницами, как реальный campaigns.get."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    def get(self, service, params):
        assert service == "campaigns"
        self.calls += 1
        limit = params["Page"]["Limit"]
        offset = params["Page"]["Offset"]
        idx = offset // limit
        items = self.pages[idx] if idx < len(self.pages) else []
        return {"Campaigns": [{"Id": i} for i in items]}


def test_fetch_campaign_ids_paginates_until_short_page(monkeypatch):
    monkeypatch.setattr(agent_e1, "CAMPAIGN_PAGE_LIMIT", 2)
    client = _FakeCampaignsClient(pages=[[1, 2], [3]])

    ids = agent_e1.fetch_campaign_ids(client)

    assert ids == [1, 2, 3]
    assert client.calls == 2  # вторая страница короче лимита — цикл остановился


def test_fetch_campaign_ids_stops_immediately_when_first_page_short():
    client = _FakeCampaignsClient(pages=[[111, 222]])  # 2 < CAMPAIGN_PAGE_LIMIT (1000)

    ids = agent_e1.fetch_campaign_ids(client)

    assert ids == [111, 222]
    assert client.calls == 1


def test_own_campaign_ids_excludes_foreign_campaigns():
    # Справочник расходов копит кампании ВСЕХ кабинетов; "999" принадлежит
    # чужому клиенту и не должна попасть в список этого кабинета.
    client = _FakeCampaignsClient(pages=[[111, 222]])
    daily_cost = {"111": 500.0, "999": 10.0}

    result = agent_e1.own_campaign_ids(client, daily_cost)

    assert result == ["111"]


def test_own_campaign_ids_drops_campaigns_without_cost_data():
    # У кабинета есть кампания 333, но по ней нет расхода в справочнике —
    # без пересечения она не должна попасть в опрос bidmodifiers.get.
    client = _FakeCampaignsClient(pages=[[111, 333]])
    daily_cost = {"111": 500.0}

    result = agent_e1.own_campaign_ids(client, daily_cost)

    assert result == ["111"]


def test_normalize_actual_splits_combined_gender_and_age():
    # Одна запись DemographicsAdjustment может нести Gender И Age одновременно
    # (ставка на пересечение сегментов) — без раскладки diff потерял бы
    # вторую половину и предложил add там, где нужен set.
    item = {"Id": 55, "DemographicsAdjustment": {
        "BidModifier": 20, "Gender": "GENDER_MALE", "Age": "AGE_25_34"}}

    out = agent_e1._normalize_actual(item)

    assert len(out) == 2
    keys = {(r["Type"], r["key"]) for r in out}
    assert keys == {
        ("DEMOGRAPHICS_ADJUSTMENT", "GENDER_MALE"),
        ("DEMOGRAPHICS_ADJUSTMENT", "AGE_25_34"),
    }
    assert all(r["Id"] == 55 and r["percent"] == 20 for r in out)


def test_normalize_actual_keeps_single_demographic_field_as_one_record():
    item = {"Id": 77, "DemographicsAdjustment": {"BidModifier": 10, "Gender": "GENDER_FEMALE"}}

    out = agent_e1._normalize_actual(item)

    assert out == [{"Id": 77, "Type": "DEMOGRAPHICS_ADJUSTMENT",
                    "key": "GENDER_FEMALE", "percent": 10}]


def test_normalize_actual_mobile_and_regional_unaffected():
    mobile = agent_e1._normalize_actual({"Id": 9, "MobileAdjustment": {"BidModifier": 15}})
    regional = agent_e1._normalize_actual(
        {"Id": 3, "RegionalAdjustment": {"BidModifier": -10, "RegionId": 213}})

    assert mobile == [{"Id": 9, "Type": "MOBILE_ADJUSTMENT", "key": "mobile", "percent": 15}]
    assert regional == [{"Id": 3, "Type": "REGIONAL_ADJUSTMENT", "key": "213", "percent": -10}]


def test_normalize_actual_no_dimensions_returns_empty():
    assert agent_e1._normalize_actual({"Id": 1}) == []
