from sync.ml.registry import REGISTRY, select_features, feature_key, FeatureSpec

def test_registry_has_no_duplicate_names():
    names = [f.name for f in REGISTRY]
    assert len(names) == len(set(names))

def test_outcome_never_selected():
    for point in ("pre_lead", "at_creation", "post_connection"):
        assert all(
            spec.availability != "outcome"
            for spec in REGISTRY
            if spec.name in select_features(point)
        )

def test_creation_excludes_post_connection():
    at_creation = set(select_features("at_creation"))
    assert "time_to_connection_days" not in at_creation  # post_connection фича
    assert "city_ip_segment" in at_creation              # at_creation фича
    assert "beh_avg_duration_sec" in at_creation         # pre_lead фича видна @creation

def test_call_filled_fields_not_at_creation():
    """audience/b24_* рождаются в звонке (96.5% у дозвонившихся vs 3.7%) —
    на at_creation это утечка дозвона, доступны только с post_connection."""
    at_creation = set(select_features("at_creation"))
    pc = set(select_features("post_connection"))
    for leak in ("audience", "b24_grad_year", "b24_edu_level"):
        assert leak not in at_creation
        assert leak in pc

def test_post_connection_is_superset_of_creation():
    assert set(select_features("at_creation")).issubset(
        set(select_features("post_connection"))
    )

def test_feature_key_maps_to_jsonb():
    assert feature_key("audience") == "f__audience"

def test_phase2_features_registered():
    atc = set(select_features("at_creation"))
    pc = set(select_features("post_connection"))
    assert "campaign_id" in atc
    assert "visits_before_lead" in atc         # pre_lead виден @creation
    assert "mins_to_connection" not in atc      # post_connection
    assert "mins_to_connection" in pc

def test_phaseb_session_features_registered():
    atc = set(select_features("at_creation"))
    pre = set(select_features("pre_lead"))
    assert "sess_utm_source" in atc
    assert "sess_direct_platform_type" in atc
    assert all(
        spec.availability != "outcome"
        for spec in REGISTRY
        if spec.name in atc
    )
    assert "sess_is_new_user" in pre
