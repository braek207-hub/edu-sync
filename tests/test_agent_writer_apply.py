# -*- coding: utf-8 -*-
"""
tests/test_agent_writer_apply.py — тесты преобразования действий в вызовы API.
"""

from sync.agent.writer.apply import to_api_call


def test_add_maps_to_bidmodifiers_add_with_mobile_shape():
    action = {"action_kind": "bidmodifier.add",
              "payload": {"CampaignId": 111, "Type": "MOBILE_ADJUSTMENT",
                          "key": "mobile", "BidModifier": 30}}
    service, method, params = to_api_call(action)
    assert (service, method) == ("bidmodifiers", "add")
    item = params["BidModifiers"][0]
    assert item["CampaignId"] == 111
    assert item["MobileAdjustment"]["BidModifier"] == 30


def test_add_maps_demographics_shape():
    action = {"action_kind": "bidmodifier.add",
              "payload": {"CampaignId": 111, "Type": "DEMOGRAPHICS_ADJUSTMENT",
                          "key": "GENDER_MALE", "BidModifier": 20}}
    service, method, params = to_api_call(action)
    item = params["BidModifiers"][0]
    assert item["DemographicsAdjustments"][0]["Gender"] == "GENDER_MALE"
    assert item["DemographicsAdjustments"][0]["BidModifier"] == 20


def test_set_maps_to_bidmodifiers_set():
    action = {"action_kind": "bidmodifier.set", "payload": {"Id": 7, "BidModifier": 30}}
    service, method, params = to_api_call(action)
    assert (service, method) == ("bidmodifiers", "set")
    assert params["BidModifiers"][0] == {"Id": 7, "BidModifier": 30}


def test_unknown_action_kind_raises():
    import pytest
    with pytest.raises(ValueError):
        to_api_call({"action_kind": "campaign.delete", "payload": {}})
