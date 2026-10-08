# -*- coding: utf-8 -*-
"""Коллекции API Директа приходят то списком, то обёрткой {"Items": [...]}.

Боевой симптом: `invalid literal for int() with base 10: 'Items'` — при обёртке
`for x in dict` перебирает КЛЮЧИ, и int('Items') роняет весь синк настроек.
Падало молча (исключение печаталось без места), настройки не синхронизировались,
state/campaign_type были заполнены у 1173 из 1603 строк.

Локализовано traceback'ом в прогоне 29653098283: sync/lime_direct.py:1828,
разбор restrictedRegionIds KZ-аккаунта.
"""
from sync import lime_direct as L
from sync import strategy_snapshots as SS
from sync.lime_direct import _as_list


def test_as_list_unwraps_items_dict():
    assert _as_list({"Items": [1, 2, 3]}) == [1, 2, 3]


def test_as_list_passes_plain_list():
    assert _as_list([1, 2]) == [1, 2]


def test_as_list_handles_none_and_empty():
    assert _as_list(None) == []
    assert _as_list({}) == []


def test_as_list_ignores_dict_without_items():
    """Словарь без Items не должен превращаться в список своих ключей."""
    assert _as_list({"Foo": "bar"}) == []


def test_int_over_wrapped_region_ids_does_not_raise():
    """Регрессия строки 1828: int() по обёрнутой коллекции регионов."""
    wrapped = {"Items": [225, 213]}
    assert [int(r) for r in _as_list(wrapped)] == [225, 213]


# --- Ниже — путь целиком, а не только _as_list: прежний фикс разворачивал обёртку у
# читателей, но источник (_fetch_adgroups_by_campaign) делал list(dict) == ['Items'],
# и _as_list(['Items']) отдавал тот же мусор. KZ падал каждый день (прогон 37734560179).


def test_adgroups_unwrap_items_at_source(monkeypatch):
    # Форма ответа adgroups.get: RegionIds — массив, RestrictedRegionIds и
    # NegativeKeywords — обёртки {"Items": [...]} (у KZ-кабинета заданы исключённые регионы).
    ag = {
        "Id": 1, "Name": "g", "CampaignId": 42, "Status": "ACCEPTED", "Type": "TEXT_AD_GROUP",
        "RegionIds": [159, -10302],
        "RestrictedRegionIds": {"Items": [977, 225]},
        "NegativeKeywords": {"Items": ["бесплатно"]},
    }
    monkeypatch.setattr(L, "_paginate_items", lambda url, key, body: [ag])
    (row,) = L._fetch_adgroups_by_campaign(["42"])["42"]
    assert row["regionIds"] == [159, -10302]
    assert row["restrictedRegionIds"] == [977, 225]
    assert row["negativeKeywords"] == ["бесплатно"]
    # Ровно то, что делала упавшая строка _sync_campaign_settings.
    assert [int(r) for r in L._as_list(row["restrictedRegionIds"])] == [977, 225]


def test_placement_types_keep_only_enabled():
    ch = L._extract_strategy_channel_full({
        "BiddingStrategyType": "AVERAGE_CPA",
        "PlacementTypes": {"SearchResults": "YES", "ProductGallery": "NO", "DynamicPlaces": "YES"},
    })
    assert ch["placementTypes"] == ["SearchResults", "DynamicPlaces"]


def _fake_direct(monkeypatch, responses):
    """responses: url -> функция(ids) -> список кампаний. Пишет журнал вызовов."""
    calls = []

    def _post(url, body):
        ids = [str(x) for x in body["params"]["SelectionCriteria"].get("Ids", [])]
        calls.append((url, ids, body["params"]["SelectionCriteria"].get("States")))
        return {"Campaigns": responses.get(url, lambda ids: [])(ids)}

    monkeypatch.setattr(L, "_direct_post", _post)
    return calls


TEXT_A = {
    "Id": 1, "Name": "A", "Type": "TEXT_CAMPAIGN", "State": "ON", "Status": "ACCEPTED",
    "TextCampaign": {"BiddingStrategy": {
        "Search": {"BiddingStrategyType": "SERVING_OFF"},
        "Network": {"BiddingStrategyType": "AVERAGE_CPA",
                    "AverageCpa": {"AverageCpa": 900_000_000, "WeeklySpendLimit": 30_000_000_000}},
    }},
}
# ЕПК, которую v5 не отдаёт, а v501 отдаёт: стратегия в UnifiedCampaign.BiddingStrategy,
# те же блоки Search/Network, что у текстовой.
UNIFIED_B = {
    "Id": 2, "Name": "B", "Type": "UNIFIED_CAMPAIGN", "State": "SUSPENDED", "Status": "ACCEPTED",
    "UnifiedCampaign": {"BiddingStrategy": {
        "Search": {"BiddingStrategyType": "SERVING_OFF"},
        "Network": {"BiddingStrategyType": "AVERAGE_CRR",
                    "AverageCrr": {"Crr": 10, "WeeklySpendLimit": 5_000_000_000}},
    }},
}


def test_missing_campaigns_are_fetched_from_v501(monkeypatch, capsys):
    calls = _fake_direct(monkeypatch, {
        L.CAMPAIGNS_URL: lambda ids: [TEXT_A] if "1" in ids else [],
        L.CAMPAIGNS_V501_URL: lambda ids: [UNIFIED_B] if "2" in ids else [],
    })
    out = L._fetch_campaigns_for_settings(["1", "2", "3"])

    assert out["2"]["meta"] == {"campaignType": "UNIFIED_CAMPAIGN", "state": "SUSPENDED", "status": "ACCEPTED"}
    row = SS.snapshot_row("2", "B", out["2"])
    assert (row["strategy_type"], row["target_crr"], row["weekly_budget"]) == ("AVERAGE_CRR", 10.0, 5000.0)
    row = SS.snapshot_row("1", "A", out["1"])
    assert (row["strategy_type"], row["target_cpa"]) == ("AVERAGE_CPA", 900.0)

    # Добор: v501 по обоим недостающим, затем v5 со всеми состояниями — только по «3».
    v501 = [c for c in calls if c[0] == L.CAMPAIGNS_V501_URL]
    assert v501[0][1] == ["2", "3"] and "ARCHIVED" in v501[0][2]
    assert calls[-1][0] == L.CAMPAIGNS_URL and calls[-1][1] == ["3"]
    # Успешный пустой ответ — не повод для поштучных запросов.
    assert len(calls) == 3
    assert "3" not in out
    assert "API не отдаёт 1 кампаний" in capsys.readouterr().out


def test_failed_retry_batch_falls_back_to_single_ids(monkeypatch):
    def _v5(ids):
        if len(ids) > 1:
            raise RuntimeError("Direct API error: 4000")
        return [dict(TEXT_A, Id=int(ids[0]))]

    calls = []

    def _post(url, body):
        ids = [str(x) for x in body["params"]["SelectionCriteria"]["Ids"]]
        calls.append((url, ids))
        if url == L.CAMPAIGNS_V501_URL:
            return {"Campaigns": []}
        if len(calls) == 1:  # первый, «основной» v5-запрос ничего не вернул
            return {"Campaigns": []}
        return {"Campaigns": _v5(ids)}

    monkeypatch.setattr(L, "_direct_post", _post)
    out = L._fetch_campaigns_for_settings(["5", "6"])
    assert {"5", "6"} <= set(out)
    assert [c[1] for c in calls[-2:]] == [["5"], ["6"]]


def test_settings_and_snapshot_cover_whole_cabinet(monkeypatch):
    """Кампания на паузе (нет показов за 7 дней) всё равно в настройках и снимке."""
    monkeypatch.setenv("LIME_DIRECT_TOKEN", "t")
    monkeypatch.setenv("LIME_DIRECT_CLIENT_LOGIN", "l")
    report = [{"date": "2026-10-07", "campaign_id": "1", "campaign_name": "A", "cost": 1.0}]
    monkeypatch.setattr(L, "_fetch_report", lambda *a: report)
    monkeypatch.setattr(L, "_fetch_video_report", lambda *a: {})
    monkeypatch.setattr(L, "_fetch_campaigns", lambda ids: {})
    monkeypatch.setattr(L, "_list_cabinet_campaigns", lambda: {"1": "A", "9": "Пауза"})
    seen = {}
    monkeypatch.setattr(L, "_sync_campaign_settings", lambda ids, names: seen.update(ids=ids, names=names))
    monkeypatch.setattr(L, "_snapshot_settings", lambda ids: seen.update(snap=ids))
    monkeypatch.setattr(L, "_upsert", lambda rows: len(rows))

    assert L.sync_lime_direct(7) == 1
    assert seen["ids"] == ["1", "9"] and seen["snap"] == ["1", "9"]
    assert seen["names"]["9"] == "Пауза"


def test_cabinet_listing_failure_keeps_report_campaigns(monkeypatch):
    monkeypatch.setenv("LIME_DIRECT_TOKEN", "t")
    monkeypatch.setenv("LIME_DIRECT_CLIENT_LOGIN", "l")
    monkeypatch.setattr(L, "_fetch_report", lambda *a: [
        {"date": "2026-10-07", "campaign_id": "1", "campaign_name": "A", "cost": 1.0}
    ])
    monkeypatch.setattr(L, "_fetch_video_report", lambda *a: {})
    monkeypatch.setattr(L, "_fetch_campaigns", lambda ids: {})

    def _boom():
        raise RuntimeError("v501 недоступен")

    monkeypatch.setattr(L, "_list_cabinet_campaigns", _boom)
    seen = {}
    monkeypatch.setattr(L, "_sync_campaign_settings", lambda ids, names: seen.update(ids=ids))
    monkeypatch.setattr(L, "_snapshot_settings", lambda ids: seen.update(snap=ids))
    monkeypatch.setattr(L, "_upsert", lambda rows: len(rows))
    L.sync_lime_direct(7)
    assert seen == {"ids": ["1"], "snap": ["1"]}


def test_cabinet_listing_asks_v501_for_non_archived(monkeypatch):
    got = {}

    def _pages(url, key, body):
        got.update(url=url, key=key, crit=body["params"]["SelectionCriteria"])
        return [{"Id": 7, "Name": "x"}]

    monkeypatch.setattr(L, "_paginate_items", _pages)
    assert L._list_cabinet_campaigns() == {"7": "x"}
    assert got["url"] == L.CAMPAIGNS_V501_URL and got["key"] == "Campaigns"
    assert "ARCHIVED" not in got["crit"]["States"]
