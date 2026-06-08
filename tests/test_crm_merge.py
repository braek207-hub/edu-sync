import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.crm import merge_leads_agg, merge_payments_agg


def test_merge_leads_agg_sums_counters():
    target = {
        "2025-01-01|123|rf|unknown|unknown": {
            "date": "2025-01-01",
            "campaign_id": "123",
            "city_ip_segment": "rf",
            "b24_grad_year": "unknown",
            "b24_edu_level": "unknown",
            "leads": 2,
            "connections": 1.0,
            "deals": 0.0,
            "project": "unknown",
            "direction": "other",
            "campaign_name": "",
        }
    }
    source = {
        "2025-01-01|123|rf|unknown|unknown": {
            "date": "2025-01-01",
            "campaign_id": "123",
            "city_ip_segment": "rf",
            "b24_grad_year": "unknown",
            "b24_edu_level": "unknown",
            "leads": 3,
            "connections": 2.0,
            "deals": 1.0,
            "project": "vse",
            "direction": "spo",
            "campaign_name": "vse_spo_msk",
        },
        "2025-01-02|456|rf|unknown|unknown": {
            "date": "2025-01-02",
            "campaign_id": "456",
            "city_ip_segment": "rf",
            "b24_grad_year": "unknown",
            "b24_edu_level": "unknown",
            "leads": 1,
            "connections": 0.0,
            "deals": 0.0,
            "project": "vuz",
            "direction": "vpo",
            "campaign_name": "",
        },
    }
    merge_leads_agg(target, source)
    row = target["2025-01-01|123|rf|unknown|unknown"]
    assert row["leads"] == 5
    assert row["connections"] == 3.0
    assert row["deals"] == 1.0
    assert row["project"] == "vse"
    assert row["direction"] == "spo"
    assert row["campaign_name"] == "vse_spo_msk"
    assert "2025-01-02|456|rf|unknown|unknown" in target


def test_merge_payments_agg_sums_revenue():
    target = {
        "2025-03-01|123|rf|unknown|unknown": {
            "date": "2025-03-01",
            "campaign_id": "123",
            "city_ip_segment": "rf",
            "b24_grad_year": "unknown",
            "b24_edu_level": "unknown",
            "payments": 1,
            "revenue": 1000.0,
            "project": "unknown",
            "direction": "other",
        }
    }
    source = {
        "2025-03-01|123|rf|unknown|unknown": {
            "date": "2025-03-01",
            "campaign_id": "123",
            "city_ip_segment": "rf",
            "b24_grad_year": "unknown",
            "b24_edu_level": "unknown",
            "payments": 2,
            "revenue": 500.0,
            "project": "provuz",
            "direction": "spo",
        }
    }
    merge_payments_agg(target, source)
    row = target["2025-03-01|123|rf|unknown|unknown"]
    assert row["payments"] == 3
    assert row["revenue"] == 1500.0
    assert row["project"] == "provuz"
