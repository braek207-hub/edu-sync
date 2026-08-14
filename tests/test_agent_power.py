from sync.agent.power import power_report


def _aggregates():
    return [
        {"campaign_id": "big", "sum_p_pay_30d": 40.0, "eff_leads_30d": 900},
        {"campaign_id": "mid", "sum_p_pay_30d": 26.0, "eff_leads_30d": 300},
        {"campaign_id": "small", "sum_p_pay_30d": 3.0, "eff_leads_30d": 40},
    ]


def test_counts_campaigns_over_decision_threshold():
    report = power_report(_aggregates())
    assert report["pass_decision_threshold"] == 2  # big и mid: Σ p_pay ≥ 25


def test_counts_campaigns_ready_for_ab():
    report = power_report(_aggregates())
    assert report["pass_ab_threshold"] == 1       # только big: ≥ 400 эфф. лидов на сплит 50/50
    assert report["campaigns_ab_ready"] == ["big"]


def test_total_counts_every_campaign():
    assert power_report(_aggregates())["total"] == 3


def test_empty_input_is_all_zeros():
    report = power_report([])
    assert report == {"total": 0, "pass_decision_threshold": 0,
                      "pass_ab_threshold": 0, "campaigns_ab_ready": []}
