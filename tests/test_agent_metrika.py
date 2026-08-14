from sync.agent.metrika import parse_campaign_behavior, parse_hourly


def test_hourly_parses_hour_and_metrics():
    data = {"data": [
        {"dimensions": [{"name": "09:00"}], "metrics": [1000.0, 25.0]},
        {"dimensions": [{"name": "10:00"}], "metrics": [1500.0, 45.0]},
    ]}
    out = parse_hourly(data)
    assert [r["segment_key"] for r in out] == ["9", "10"]
    assert out[0]["clicks"] == 1000
    assert out[0]["sum_p_pay"] == 25.0
    assert all(r["segment_kind"] == "hour" for r in out)


def test_hourly_handles_midnight_and_plain_numbers():
    data = {"data": [
        {"dimensions": [{"name": "00"}], "metrics": [10.0, 1.0]},
        {"dimensions": [{"name": "23"}], "metrics": [20.0, 2.0]},
    ]}
    out = parse_hourly(data)
    assert [r["segment_key"] for r in out] == ["0", "23"]


def test_hourly_sorted_numerically_not_lexically():
    data = {"data": [
        {"dimensions": [{"name": "21"}], "metrics": [1.0, 0.0]},
        {"dimensions": [{"name": "3"}], "metrics": [1.0, 0.0]},
    ]}
    assert [r["segment_key"] for r in parse_hourly(data)] == ["3", "21"]


def test_hourly_skips_rows_without_metrics():
    data = {"data": [{"dimensions": [{"name": "09"}], "metrics": []}]}
    assert parse_hourly(data) == []


def test_behavior_converts_rates_to_counts():
    # Храним суммы и счётчики: среднее по среднему не складывается
    # при перегруппировке по неделям и направлениям.
    data = {"data": [
        {"dimensions": [{"name": "12345"}], "metrics": [200.0, 25.0, 3.5, 120.0]},
    ]}
    out = parse_campaign_behavior(data)
    assert len(out) == 1
    row = out[0]
    assert row["campaign_id"] == "12345"
    assert row["visits"] == 200
    assert row["bounces"] == 50          # 25% от 200
    assert row["pageviews"] == 700       # 3.5 × 200
    assert row["visit_seconds"] == 24000  # 120 × 200


def test_behavior_skips_non_numeric_campaign():
    data = {"data": [
        {"dimensions": [{"name": "не задано"}], "metrics": [10.0, 0.0, 1.0, 5.0]},
        {"dimensions": [{"name": "777"}], "metrics": [10.0, 0.0, 1.0, 5.0]},
    ]}
    assert [r["campaign_id"] for r in parse_campaign_behavior(data)] == ["777"]


def test_behavior_skips_incomplete_rows():
    data = {"data": [{"dimensions": [{"name": "777"}], "metrics": [10.0, 0.0]}]}
    assert parse_campaign_behavior(data) == []


def test_empty_response():
    assert parse_hourly({}) == []
    assert parse_campaign_behavior({}) == []
