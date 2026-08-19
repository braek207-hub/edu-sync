# -*- coding: utf-8 -*-
from sync.agent.writer.plan import SETTING_KIND_MAP, desired_bid_modifiers


def _row(kind, key, value, support=1000):
    return {"setting_kind": kind, "setting_key": key, "value": value, "support_n": support}


def test_maps_computed_kinds_to_direct_types():
    assert SETTING_KIND_MAP["bid_modifier:device"] == "MOBILE_ADJUSTMENT"
    assert SETTING_KIND_MAP["bid_modifier:gender"] == "DEMOGRAPHICS_ADJUSTMENT"
    assert SETTING_KIND_MAP["bid_modifier:age"] == "DEMOGRAPHICS_ADJUSTMENT"


def test_keeps_meaningful_modifiers():
    out = desired_bid_modifiers([_row("bid_modifier:device", "mobile", 30.0)])
    assert len(out) == 1
    assert out[0]["percent"] == 30


def test_drops_near_zero_modifiers():
    # Корректировка ±4% не стоит запроса к API и риска: шум.
    out = desired_bid_modifiers([_row("bid_modifier:device", "mobile", 4.0)])
    assert out == []


def test_drops_low_support_rows():
    # Мало наблюдений — сжатие уже увело значение к нулю, но подстраховываемся явно.
    out = desired_bid_modifiers([_row("bid_modifier:device", "mobile", 30.0, support=10)])
    assert out == []


def test_ignores_unknown_setting_kinds():
    out = desired_bid_modifiers([_row("schedule:hour", "9", 30.0)])
    assert out == []


def test_percent_is_integer():
    out = desired_bid_modifiers([_row("bid_modifier:device", "mobile", 30.7)])
    assert isinstance(out[0]["percent"], int)


def test_empty_input():
    assert desired_bid_modifiers([]) == []
