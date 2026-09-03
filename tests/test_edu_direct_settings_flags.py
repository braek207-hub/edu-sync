# -*- coding: utf-8 -*-
"""campaignSettings был пуст у всех 147 кампаний прода (проверено 03.09.2026):
разбор читал ключи Name/Option, а CampaignSettingGet API v5 отдаёт
{"Option": <флаг>, "Value": "YES"|"NO"}. Из-за этого нельзя было ответить
даже на вопрос «включён ли расширенный геотаргетинг».
"""

from sync import edu_direct_settings as S


def test_campaign_settings_flags_are_read_from_option_value(monkeypatch):
    campaign = {
        "Id": 111, "Name": "к", "Type": "TEXT_CAMPAIGN", "State": "ON", "Status": "ACCEPTED",
        "TextCampaign": {
            "Settings": [
                {"Option": "ENABLE_AREA_OF_INTEREST_TARGETING", "Value": "YES"},
                {"Option": "ALTERNATIVE_TEXTS_ENABLED", "Value": "NO"},
                {"Option": "ENABLE_SITE_MONITORING", "Value": "YES"},
            ],
            "BiddingStrategy": {
                "Search": {"BiddingStrategyType": "HIGHEST_POSITION"},
                "Network": {"BiddingStrategyType": "SERVING_OFF"},
            },
        },
    }
    monkeypatch.setattr(S, "_direct_post", lambda url, body: {"Campaigns": [campaign]})
    monkeypatch.setattr(S, "_fetch_package_strategies", lambda ids: {}, raising=False)

    out = S._fetch_campaigns_for_settings(["111"])

    assert out["111"]["targeting"]["campaignSettings"] == [
        "ENABLE_AREA_OF_INTEREST_TARGETING",
        "ENABLE_SITE_MONITORING",
    ]
