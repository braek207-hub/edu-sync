# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.brand_terms import brand_regex, is_brand_query, terms_for


def test_base_terms_shared_by_ru_and_kz():
    assert terms_for("ru") == terms_for("kz")
    assert "лайм" in terms_for("kz")
    assert "дшьу" in terms_for("kz")  # lime вслепую на русской раскладке


def test_gcc_adds_arabic_and_keeps_cyrillic():
    gcc = terms_for("gcc")
    assert "لايم" in gcc          # основной арабский вариант
    assert "ليم" in gcc           # краткий, ловит и ليمي
    assert "leem" in gcc          # транслитерация
    assert "лайм" in gcc          # русскоязычные покупатели Залива


def test_unknown_region_falls_back_to_base():
    assert terms_for("zz") == terms_for("ru")


def test_is_brand_query_matches_local_spellings():
    assert is_brand_query("lime uae", "gcc")
    assert is_brand_query("محل لايم", "gcc")      # арабский вариант
    assert is_brand_query("لايم ملابس", "gcc")
    assert is_brand_query("дшьу", "kz")           # слепая раскладка
    assert is_brand_query("лайм магазин", "kz")
    assert is_brand_query("LIME Store", "gcc")    # регистр не важен


def test_is_brand_query_rejects_non_brand():
    assert not is_brand_query("linen pants", "gcc")
    assert not is_brand_query("blazer", "gcc")
    assert not is_brand_query("платье", "kz")
    assert not is_brand_query("", "kz")


def test_arabic_terms_do_not_leak_into_kz_matching():
    # арабского в KZ-наборе нет → запрос не считается брендовым
    assert not is_brand_query("لايم", "kz")


def test_brand_regex_is_case_insensitive_alternation():
    rx = brand_regex("gcc")
    assert rx.startswith("(?i)(")
    assert "|" in rx
    assert "لايم" in rx
