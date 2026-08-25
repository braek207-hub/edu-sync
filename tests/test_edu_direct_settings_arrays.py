# -*- coding: utf-8 -*-
"""Две поломки синка настроек, вскрытые замером 25.08.2026 (run 32869597656).

API Директа отдаёт списки в двух формах: голым массивом и обёрткой
{"Items": [...]}. Разбор групп брал форму на веру — list({"Items": [...]})
даёт ["Items"], и дальше int("Items") ронял синк трёх кабинетов из четырёх.
Прогон при этом оставался зелёным: исключение ловилось общим except и
печаталось предупреждением. Витрина настроек жила на данных прошлых удачных
прогонов, а новые кампании в неё не попадали — отсюда «слепая зона» агента.
"""

import pytest

from sync import edu_direct_settings as S


def _adgroup(region_ids, restricted, negative):
    return {
        "Id": 1, "Name": "г", "CampaignId": 111, "Status": "ACCEPTED",
        "Type": "TEXT_AD_GROUP",
        "RegionIds": region_ids,
        "RestrictedRegionIds": restricted,
        "NegativeKeywords": negative,
    }


@pytest.mark.parametrize("wrap", [
    lambda v: v,                    # голый список
    lambda v: {"Items": v},         # обёртка API
])
def test_adgroup_arrays_survive_both_api_forms(monkeypatch, wrap):
    monkeypatch.setattr(S, "_paginate_items",
                        lambda url, key, body, limit=1000: [
                            _adgroup(wrap([225, 1]), wrap([977]), wrap(["бесплатно"]))
                        ])
    out = S._fetch_adgroups_by_campaign(["111"])
    ag = out["111"][0]
    assert ag["regionIds"] == [225, 1]
    assert ag["restrictedRegionIds"] == [977]
    assert ag["negativeKeywords"] == ["бесплатно"]
    # Главное следствие: числа остаются числами. Раньше сюда приезжала строка
    # "Items", и int() ронял весь кабинет.
    assert all(isinstance(r, int) for r in ag["regionIds"])


def test_missing_arrays_give_empty_lists(monkeypatch):
    monkeypatch.setattr(S, "_paginate_items",
                        lambda url, key, body, limit=1000: [_adgroup(None, None, None)])
    ag = S._fetch_adgroups_by_campaign(["111"])["111"][0]
    assert ag["regionIds"] == []
    assert ag["restrictedRegionIds"] == []
    assert ag["negativeKeywords"] == []


def test_failed_account_makes_the_run_red(monkeypatch, capsys):
    # Кабинеты независимы, поэтому падение одного не отменяет остальные: витрина
    # частичная лучше пустой. Но прогон обязан кончиться красным.
    monkeypatch.setattr(S, "_direct_clients",
                        lambda: [{"login": "a"}, {"login": "b"}], raising=False)
    monkeypatch.setattr("sync.direct._direct_clients",
                        lambda: [{"login": "a"}, {"login": "b"}])
    monkeypatch.setattr(S, "_list_campaigns_for_login", lambda: {"111": "к"})

    def _sync(campaign_ids, names):
        if S._CURRENT_LOGIN == "a":
            raise ValueError("invalid literal for int() with base 10: 'Items'")
        return 7

    monkeypatch.setattr(S, "_sync_campaign_settings", _sync)
    monkeypatch.setattr(S, "_snapshot_strategies", lambda: 0)

    with pytest.raises(RuntimeError) as err:
        S.sync_edu_campaign_settings()
    assert "a" in str(err.value)
    out = capsys.readouterr().out
    # Второй кабинет всё-таки записан, а первый назван виновником в логе.
    assert "настройки 7 кампаний" in out
    assert "ОШИБКА настройки не синхронизированы" in out
