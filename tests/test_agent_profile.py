from sync.agent.profile import build_profile, campaign_quality, distance_to_profile

FEATURES = ["groups_count", "phrases_per_group", "title2_fill_share"]


def _campaigns():
    rows = []
    for i in range(20):
        good = i < 10
        rows.append({
            "campaign_id": f"c{i}",
            "cost": 100000.0,
            "sum_p_pay": 40.0 if good else 10.0,
            "groups_count": 30 if good else 5,
            "phrases_per_group": 12 if good else 90,
            "title2_fill_share": 0.95 if good else 0.4,
        })
    return rows


def test_quality_is_cost_per_expected_payment():
    assert campaign_quality({"cost": 1000.0, "sum_p_pay": 10.0}) == 100.0


def test_quality_is_none_without_expected_payments():
    assert campaign_quality({"cost": 1000.0, "sum_p_pay": 0.0}) is None


def test_profile_captures_top_quartile_medians():
    profile = build_profile(_campaigns(), FEATURES)
    # Верхний квартиль — «хорошие»: много групп, мало фраз в группе, заполнен Заголовок 2.
    assert profile["groups_count"] > 20
    assert profile["phrases_per_group"] < 30
    assert profile["title2_fill_share"] > 0.8


def test_profile_ignores_campaigns_without_quality():
    rows = _campaigns() + [{"campaign_id": "x", "cost": 5.0, "sum_p_pay": 0.0,
                            "groups_count": 1, "phrases_per_group": 999, "title2_fill_share": 0.0}]
    profile = build_profile(rows, FEATURES)
    assert profile["phrases_per_group"] < 30


def test_distance_is_zero_for_profile_matching_campaign():
    profile = build_profile(_campaigns(), FEATURES)
    perfect = {"campaign_id": "p", **{f: profile[f] for f in FEATURES}}
    distance, gaps = distance_to_profile(perfect, profile, FEATURES)
    assert distance == 0.0
    assert gaps == []


def test_distance_lists_worst_gaps_first():
    profile = build_profile(_campaigns(), FEATURES)
    bad = {"campaign_id": "b", "groups_count": 1, "phrases_per_group": 300, "title2_fill_share": 0.1}
    distance, gaps = distance_to_profile(bad, profile, FEATURES)
    assert distance > 0
    assert gaps[0]["feature"] in FEATURES
    assert gaps[0]["gap"] >= gaps[-1]["gap"]


def test_empty_input_gives_empty_profile():
    assert build_profile([], FEATURES) == {}
