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


def test_unknown_setting_kind_is_reported_as_unsupported():
    # Прежде вид настройки, которого нет ни в одном справочнике, выпадал
    # МОЛЧА: строка не попадала ни в desired, ни в unsupported, и отчёт
    # прогона был неотличим от «таких данных нет». Так исчезал не только
    # schedule:* (его пауза осознанна), но и bid_modifier:network —
    # посчитанный, значимый и никем не замеченный. Отказ обязан быть громким.
    report = plan_bid_modifiers([_row("schedule:hour", "9", 30.0)])

    assert report["desired"] == []
    assert len(report["unsupported"]) == 1
    assert report["unsupported"][0]["kind"] == "schedule:hour"
    assert "schedule:hour" in report["unsupported"][0]["reason"]


def test_unknown_bid_modifier_kind_is_not_lost_silently():
    # Обратная половина того же дефекта и его настоящая цена: сетевой срез
    # считается тем же движком (sync/agent/segments.py::SEGMENT_FIELDS), его
    # корректировки значимы, а применить их Э1a не умеет — и до правки об
    # этом нельзя было узнать ниоткуда.
    report = plan_bid_modifiers([_row("bid_modifier:network", "SEARCH", 30.0)])

    assert report["desired"] == []
    assert [r["kind"] for r in report["unsupported"]] == ["bid_modifier:network"]
    assert report["unsupported"][0]["reason"]


def test_percent_is_integer():
    out = desired_bid_modifiers([_row("bid_modifier:device", "MOBILE", 30.7)])
    assert isinstance(out[0]["percent"], int)


def test_empty_input():
    assert desired_bid_modifiers([]) == []


# =========================================================================
# Дефект И2: демографические ключи уходили в API без списка допустимых
#
# У устройств список есть (DEVICE_TYPE_MAP), у регионов — проверка на числовой
# RegionId, а у пола и возраста не было ничего: любой ключ проходил план
# насквозь и попадал в тело запроса.
# =========================================================================


def test_allowed_demographic_keys_come_from_the_working_code():
    # Перечни не выдуманы: это те же списки, по которым рабочий код проекта
    # РАЗБИРАЕТ корректировки, прочитанные из кабинета. Разойдись они — и
    # движок писал бы значения, которых сам же не понимает при чтении.
    from sync.agent.writer.plan import AGE_KEYS, GENDER_KEYS
    from sync.edu_direct_settings import _AGE_RU, _GENDER_RU

    assert set(GENDER_KEYS) == set(_GENDER_RU)
    assert set(AGE_KEYS) == set(_AGE_RU)


def test_undefined_gender_is_unsupported_not_planned():
    # Отчёты Директа штатно отдают сегмент «не определено» (UNKNOWN), а
    # bidmodifiers.add такого значения не принимает. Порог по объёму
    # наблюдений такой сегмент проходит легко — значит без списка он
    # доезжал до API, получал отказ уровня элемента и переотправлялся вечно.
    report = plan_bid_modifiers([_row("bid_modifier:gender", "UNKNOWN", 30.0)])

    assert report["desired"] == []
    assert len(report["unsupported"]) == 1
    assert "UNKNOWN" in report["unsupported"][0]["reason"]


def test_undefined_age_is_unsupported_not_planned():
    report = plan_bid_modifiers([_row("bid_modifier:age", "UNKNOWN", 30.0)])

    assert report["desired"] == []
    assert len(report["unsupported"]) == 1


def test_gender_value_is_not_accepted_as_age_and_back():
    # Список работает В ОБЕ СТОРОНЫ: значение пола под видом возраста —
    # такой же неизвестный ключ, как UNKNOWN, и уходит в «неподдерживаемые».
    assert direct_type_for("bid_modifier:age", "GENDER_MALE")[0] is None
    assert direct_type_for("bid_modifier:gender", "AGE_25_34")[0] is None


def test_known_demographic_keys_still_pass():
    # Обратная половина: список не должен глушить нормальные сегменты.
    for kind, key in (("bid_modifier:gender", "GENDER_MALE"),
                      ("bid_modifier:gender", "GENDER_FEMALE"),
                      ("bid_modifier:age", "AGE_0_17"),
                      ("bid_modifier:age", "AGE_55")):
        direct_type, canonical, reason = direct_type_for(kind, key)
        assert direct_type == "DEMOGRAPHICS_ADJUSTMENT", (kind, key)
        assert canonical == key and reason == ""


def test_demographic_key_is_canonicalised_to_upper_case():
    # Регистр канонизируется так же, как у устройств: иначе план
    # ("gender_male") и факт из API ("GENDER_MALE") не сойдутся по паре
    # (тип, ключ) никогда, и diff вечно предлагал бы add вместо set.
    assert direct_type_for("bid_modifier:gender", " gender_male ")[1] == "GENDER_MALE"


def test_refusal_always_carries_a_reason():
    # Пустая причина при отказе означала бы молчаливое выпадение строки.
    for kind, key in (("bid_modifier:gender", "UNKNOWN"),
                      ("bid_modifier:age", "UNKNOWN"),
                      ("bid_modifier:device", "TV"),
                      ("bid_modifier:region", "Москва"),
                      ("schedule:hour", "9"),
                      ("совсем незнакомый вид", "x")):
        direct_type, _, reason = direct_type_for(kind, key)
        assert direct_type is None, (kind, key)
        assert reason, (kind, key)


# --------------- DESKTOP и TABLET несовместимы в одной кампании
# Установлено экспериментом (32559366898 → 32561294615 → 32561534117), вопреки
# справочнику Яндекса: планшет поверх десктопной корректировки отвергается с
# Code 6000 «Условия в корректировках пересекаются», а он же в кампанию без
# десктопной — принимается. Гипотеза «слать набор одним запросом» проверена и
# опровергнута: планшет отвергли и в паре с мобильным, мобильный приняли.

def test_desktop_and_tablet_are_never_planned_together():
    plan = plan_bid_modifiers([
        _row("bid_modifier:device", "DESKTOP", -12.0, support=5000),
        _row("bid_modifier:device", "TABLET", -38.0, support=800),
    ])
    keys = {r["key"] for r in plan["desired"]}

    assert keys == {"DESKTOP"}, "обе корректировки сразу Директ не принимает"


def test_crowded_out_device_is_named_not_dropped_silently():
    # «Планшет не нужен» и «планшет вытеснен» обязаны различаться в отчёте:
    # иначе исчезновение сегмента выглядит как отсутствие данных по нему.
    plan = plan_bid_modifiers([
        _row("bid_modifier:device", "DESKTOP", -12.0, support=5000),
        _row("bid_modifier:device", "TABLET", -38.0, support=800),
    ])
    out = [r for r in plan["unsupported"] if r["key"] == "TABLET"]

    assert len(out) == 1
    assert "несовместим" in out[0]["reason"]
    assert out[0]["percent"] == -38


def test_larger_sample_wins_the_device_slot():
    # Выигрывает сегмент с бОльшим объёмом наблюдений: там оценка надёжнее.
    plan = plan_bid_modifiers([
        _row("bid_modifier:device", "DESKTOP", -12.0, support=300),
        _row("bid_modifier:device", "TABLET", -38.0, support=9000),
    ])

    assert {r["key"] for r in plan["desired"]} == {"TABLET"}
    assert [r["key"] for r in plan["unsupported"]] == ["DESKTOP"]


def test_tie_keeps_desktop_deterministically():
    # При равном объёме — DESKTOP: он покрывает больше трафика. Выбор обязан
    # быть детерминированным, иначе один расчёт даёт разные планы.
    plan = plan_bid_modifiers([
        _row("bid_modifier:device", "DESKTOP", -12.0, support=1000),
        _row("bid_modifier:device", "TABLET", -38.0, support=1000),
    ])

    assert {r["key"] for r in plan["desired"]} == {"DESKTOP"}


def test_mobile_is_not_touched_by_the_exclusion():
    # Мобильный с этой парой не конфликтует: в опыте 32561294615 его приняли
    # тем же запросом, которым отвергли планшет.
    plan = plan_bid_modifiers([
        _row("bid_modifier:device", "DESKTOP", -12.0, support=5000),
        _row("bid_modifier:device", "TABLET", -38.0, support=800),
        _row("bid_modifier:device", "MOBILE", 19.0, support=9000),
    ])

    assert {r["key"] for r in plan["desired"]} == {"DESKTOP", "MOBILE"}


def test_single_device_is_planned_as_before():
    # Пары нет — вытеснять нечего, поведение прежнее.
    plan = plan_bid_modifiers([_row("bid_modifier:device", "TABLET", -38.0)])

    assert [r["key"] for r in plan["desired"]] == ["TABLET"]
    assert plan["unsupported"] == []
