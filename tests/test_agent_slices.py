from sync.agent.slices import build_sliced_facts, collapse_tail, to_week_start


def test_week_start_is_monday():
    assert to_week_start("2026-08-14") == "2026-08-10"  # четверг → понедельник
    assert to_week_start("2026-08-10") == "2026-08-10"  # сам понедельник


def test_build_aggregates_days_into_weeks():
    rows = [
        {"campaign_id": "1", "date": "2026-08-10", "slice_key": "mobile",
         "cost": 100.0, "clicks": 10, "impressions": 500, "conversions": 1},
        {"campaign_id": "1", "date": "2026-08-12", "slice_key": "mobile",
         "cost": 50.0, "clicks": 5, "impressions": 200, "conversions": 0},
    ]
    out = build_sliced_facts(rows, "device")
    assert len(out) == 1
    assert out[0]["week_start"] == "2026-08-10"
    assert out[0]["cost"] == 150.0
    assert out[0]["clicks"] == 15
    assert out[0]["slice_kind"] == "device"


def test_build_keeps_weeks_separate():
    rows = [
        {"campaign_id": "1", "date": "2026-08-10", "slice_key": "mobile",
         "cost": 100.0, "clicks": 10, "impressions": 500, "conversions": 1},
        {"campaign_id": "1", "date": "2026-08-17", "slice_key": "mobile",
         "cost": 100.0, "clicks": 10, "impressions": 500, "conversions": 1},
    ]
    assert len(build_sliced_facts(rows, "device")) == 2


def test_collapse_tail_merges_small_keys_into_other():
    rows = [
        {"campaign_id": "1", "week_start": "2026-08-10", "slice_kind": "region",
         "slice_key": "msk", "cost": 1000.0, "clicks": 500, "impressions": 9000, "conversions": 10},
        {"campaign_id": "1", "week_start": "2026-08-10", "slice_kind": "region",
         "slice_key": "tver", "cost": 10.0, "clicks": 2, "impressions": 90, "conversions": 0},
        {"campaign_id": "1", "week_start": "2026-08-10", "slice_kind": "region",
         "slice_key": "pskov", "cost": 5.0, "clicks": 1, "impressions": 40, "conversions": 0},
    ]
    out = collapse_tail(rows, min_clicks=30)
    keys = {r["slice_key"] for r in out}
    assert keys == {"msk", "other"}
    other = next(r for r in out if r["slice_key"] == "other")
    assert other["clicks"] == 3
    assert other["cost"] == 15.0


def test_collapse_tail_keeps_everything_when_all_large():
    rows = [
        {"campaign_id": "1", "week_start": "2026-08-10", "slice_kind": "region",
         "slice_key": k, "cost": 100.0, "clicks": 100, "impressions": 900, "conversions": 1}
        for k in ("msk", "spb")
    ]
    assert len(collapse_tail(rows, min_clicks=30)) == 2


def test_collapse_tail_does_not_mix_campaigns_or_weeks():
    rows = [
        {"campaign_id": "1", "week_start": "2026-08-10", "slice_kind": "region",
         "slice_key": "tver", "cost": 10.0, "clicks": 2, "impressions": 90, "conversions": 0},
        {"campaign_id": "2", "week_start": "2026-08-10", "slice_kind": "region",
         "slice_key": "tver", "cost": 10.0, "clicks": 2, "impressions": 90, "conversions": 0},
    ]
    out = collapse_tail(rows, min_clicks=30)
    assert len(out) == 2
    assert {r["campaign_id"] for r in out} == {"1", "2"}


def test_empty_input():
    assert build_sliced_facts([], "device") == []
    assert collapse_tail([], min_clicks=30) == []
