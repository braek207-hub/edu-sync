from sync.edu_visits_logs import map_row


def test_map_row_selects_fields_and_buckets_phrase():
    header = ["ym:s:dateTime", "ym:s:clientID", "ym:s:visitID", "ym:s:lastDirectPhraseOrCond", "ym:s:isNewUser"]
    cols = ["2026-07-20 13:59:21", "123", "v1", "очень_редкая_фраза", "1"]
    row = map_row(header, cols, {"direct_phrase": set()})  # пустой allowed → phrase='other'
    assert row["client_id"] == "123" and row["visit_id"] == "v1"
    assert row["visit_ts"].startswith("2026-07-20 13:59:21")
    assert row["direct_phrase"] == "other"
    assert row["is_new_user"] == 1


def test_map_row_missing_fields_are_none_not_crash():
    header = ["ym:s:dateTime", "ym:s:clientID", "ym:s:visitID"]
    cols = ["2026-07-20 13:59:21", "123", "v1"]
    row = map_row(header, cols, {})
    assert row["utm_source"] is None
    assert row["visit_duration"] is None
    assert row["has_gclid"] is None


def test_map_row_never_includes_goal_fields():
    header = ["ym:s:dateTime", "ym:s:clientID", "ym:s:visitID"]
    cols = ["2026-07-20 13:59:21", "123", "v1"]
    row = map_row(header, cols, {})
    assert "goals_id" not in row
    assert not any("goal" in k.lower() for k in row.keys())
