# -*- coding: utf-8 -*-
from sync.agent.writer.plan import (
    DEVICE_TYPE_MAP,
    SETTING_KIND_MAP,
    desired_bid_modifiers,
    direct_type_for,
    plan_bid_modifiers,
)


def _row(kind, key, value, support=1000):
    return {"setting_kind": kind, "setting_key": key, "value": value, "support_n": support}


def test_maps_computed_kinds_to_direct_types():
    assert SETTING_KIND_MAP["bid_modifier:gender"] == "DEMOGRAPHICS_ADJUSTMENT"
    assert SETTING_KIND_MAP["bid_modifier:age"] == "DEMOGRAPHICS_ADJUSTMENT"
    assert SETTING_KIND_MAP["bid_modifier:region"] == "REGIONAL_ADJUSTMENT"


def test_device_type_depends_on_key_not_on_kind():
    # У Директа устройства — ТРИ разных типа корректировки; вид настройки
    # один и тот же, различает их только ключ.
    assert DEVICE_TYPE_MAP == {
        "DESKTOP": "DESKTOP_ADJUSTMENT",
        "MOBILE": "MOBILE_ADJUSTMENT",
        "TABLET": "TABLET_ADJUSTMENT",
    }
    assert direct_type_for("bid_modifier:device", "DESKTOP")[0] == "DESKTOP_ADJUSTMENT"
    assert direct_type_for("bid_modifier:device", "MOBILE")[0] == "MOBILE_ADJUSTMENT"


def test_device_key_is_canonicalized_to_upper_case():
    # Ключ плана и ключ нормализованного факта обязаны совпадать по форме,
    # иначе diff не сойдётся никогда.
    out = desired_bid_modifiers([_row("bid_modifier:device", "mobile", 30.0)])
    assert out[0]["key"] == "MOBILE"


def test_keeps_meaningful_modifiers():
    out = desired_bid_modifiers([_row("bid_modifier:device", "MOBILE", 30.0)])
    assert len(out) == 1
    assert out[0]["percent"] == 30


def test_drops_near_zero_modifiers():
    # Корректировка ±4% не стоит запроса к API и риска: шум.
    out = desired_bid_modifiers([_row("bid_modifier:device", "MOBILE", 4.0)])
    assert out == []


def test_drops_low_support_rows():
    # Мало наблюдений — сжатие уже увело значение к нулю, но подстраховываемся явно.
    out = desired_bid_modifiers([_row("bid_modifier:device", "MOBILE", 30.0, support=10)])
    assert out == []


def test_ignores_unknown_setting_kinds():
    out = desired_bid_modifiers([_row("schedule:hour", "9", 30.0)])
    assert out == []


def test_unknown_setting_kind_is_not_reported_as_unsupported():
    # schedule:* — не «сломалось», а «сюда не относится»: в отчёт о
    # неприменимых настройках такое попадать не должно.
    assert plan_bid_modifiers([_row("schedule:hour", "9", 30.0)])["unsupported"] == []


def test_percent_is_integer():
    out = desired_bid_modifiers([_row("bid_modifier:device", "MOBILE", 30.7)])
    assert isinstance(out[0]["percent"], int)


def test_empty_input():
    assert desired_bid_modifiers([]) == []
