from sync.agent.computed import (
    bid_modifier_percent,
    compute_schedule,
    compute_segment_modifiers,
    shrink_ratio,
)


def test_shrink_pulls_small_sample_to_base():
    # 2 наблюдения с конверсией втрое выше базы — почти полностью сжимается.
    out = shrink_ratio(segment_conv=0.06, segment_n=2, base_conv=0.02)
    assert 1.0 <= out <= 1.15


def test_shrink_trusts_large_sample():
    out = shrink_ratio(segment_conv=0.06, segment_n=5000, base_conv=0.02)
    assert out > 2.5


def test_shrink_returns_one_on_zero_support():
    assert shrink_ratio(0.06, 0, 0.02) == 1.0


def test_shrink_returns_one_on_zero_base():
    assert shrink_ratio(0.06, 100, 0.0) == 1.0


def test_modifier_percent_is_rounded_and_capped():
    assert bid_modifier_percent(1.3) == 30
    assert bid_modifier_percent(0.8) == -20
    assert bid_modifier_percent(3.0, cap=0.5) == 50     # потолок +50%
    assert bid_modifier_percent(0.1, cap=0.5) == -50    # пол −50%


def test_modifier_percent_neutral_is_zero():
    assert bid_modifier_percent(1.0) == 0


def test_compute_segment_modifiers_emits_rows():
    rows = [
        {"segment_kind": "device", "segment_key": "mobile", "leads": 1000,
         "sum_p_pay": 30.0, "clicks": 50000},
        {"segment_kind": "device", "segment_key": "desktop", "leads": 200,
         "sum_p_pay": 2.0, "clicks": 10000},
    ]
    out = compute_segment_modifiers(rows, base_conv=0.02)
    assert {r["setting_key"] for r in out} == {"mobile", "desktop"}
    assert all(r["setting_kind"] == "bid_modifier:device" for r in out)
    assert all(isinstance(r["value"], (int, float)) for r in out)


def test_compute_segment_modifiers_skips_empty_segments():
    rows = [{"segment_kind": "device", "segment_key": "tv", "leads": 0,
             "sum_p_pay": 0.0, "clicks": 0}]
    assert compute_segment_modifiers(rows, base_conv=0.02) == []


def test_compute_schedule_covers_all_hours_present():
    rows = [
        {"segment_kind": "hour", "segment_key": str(h), "leads": 100,
         "sum_p_pay": 2.0, "clicks": 5000}
        for h in range(24)
    ]
    out = compute_schedule(rows, base_conv=0.02)
    assert len(out) == 24
    assert all(r["setting_kind"] == "schedule:hour" for r in out)
