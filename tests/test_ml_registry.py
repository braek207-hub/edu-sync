from sync.ml.registry import REGISTRY, select_features, FeatureSpec

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
    assert "audience" in at_creation                     # at_creation фича
    assert "beh_avg_duration_sec" in at_creation         # pre_lead фича видна @creation

def test_post_connection_is_superset_of_creation():
    assert set(select_features("at_creation")).issubset(
        set(select_features("post_connection"))
    )
