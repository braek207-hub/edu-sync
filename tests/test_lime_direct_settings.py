# -*- coding: utf-8 -*-
"""Коллекции API Директа приходят то списком, то обёрткой {"Items": [...]}.

Боевой симптом: `invalid literal for int() with base 10: 'Items'` — при обёртке
`for x in dict` перебирает КЛЮЧИ, и int('Items') роняет весь синк настроек.
Падало молча (исключение печаталось без места), настройки не синхронизировались,
state/campaign_type были заполнены у 1173 из 1603 строк.

Локализовано traceback'ом в прогоне 29653098283: sync/lime_direct.py:1828,
разбор restrictedRegionIds KZ-аккаунта.
"""
from sync.lime_direct import _as_list


def test_as_list_unwraps_items_dict():
    assert _as_list({"Items": [1, 2, 3]}) == [1, 2, 3]


def test_as_list_passes_plain_list():
    assert _as_list([1, 2]) == [1, 2]


def test_as_list_handles_none_and_empty():
    assert _as_list(None) == []
    assert _as_list({}) == []


def test_as_list_ignores_dict_without_items():
    """Словарь без Items не должен превращаться в список своих ключей."""
    assert _as_list({"Foo": "bar"}) == []


def test_int_over_wrapped_region_ids_does_not_raise():
    """Регрессия строки 1828: int() по обёрнутой коллекции регионов."""
    wrapped = {"Items": [225, 213]}
    assert [int(r) for r in _as_list(wrapped)] == [225, 213]
