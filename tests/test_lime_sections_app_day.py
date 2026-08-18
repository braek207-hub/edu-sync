# -*- coding: utf-8 -*-
"""Смоук build_app_day на фейковом стриме: агрегация без сети.

NameError в app-ветке (camps/camp) доехал до CI, потому что app-конвейер не
исполнялся ни одним тестом (очередь AppMetrica три дня не отдавала выгрузки).
Этот тест гоняет полный проход дня на фикстурах — ловит опечатки и рассинхрон
форм строк с lime_sections_db.TABLES.
"""
import json
from datetime import datetime
from unittest.mock import patch

from sync import lime_sections_app as A
from sync.lime_sections_db import TABLES


class FakeFeed:
    def type_of_article(self, art):
        return "Брюки"


class FakeResolver:
    fm = FakeFeed()

    def section_of_article(self, art):
        return "men"

    def gender_of_item(self, it):
        return "men"


class FakeAttr:
    def publisher(self, dev, when):
        return "Yandex.Direct Auto-Tracking"

    def campaign(self, dev, when):
        return "direct:118498117"

    def source(self, dev, when):
        return "paid"


def fake_stream(day, event, token):
    rows = {
        "view_item": [["d1", f"{day} 10:00:00", '{"item_article":"ART1"}']],
        "section_tab_show": [["d2", f"{day} 11:00:00", '{"section_type":"men"}']],
        "add_to_cart": [["d1", f"{day} 11:30:00",
                         json.dumps({"items": [{"item_article": "ART1", "quantity": 1, "price": 100}]})]],
        "purchase": [["d1", f"{day} 12:00:00",
                      json.dumps({"transaction_id": "tx1",
                                  "items": [{"item_article": "ART1", "quantity": 2, "price": 1500}]})]],
    }
    yield from rows[event]


def test_build_app_day_full_pass():
    with patch.object(A, "_stream_event", fake_stream):
        sets = A.build_app_day("2026-08-03", "tok", FakeResolver(), FakeAttr(), set())

    # Формы строк совпадают с колонками целевых таблиц (nb_orders — не таблица).
    for key, rows in sets.items():
        if key in ("nb_orders", "cohort_input"):
            continue
        cols = TABLES[key][1]
        for row in rows:
            extra = 2 if key == "campaign" else 0  # new-колонки добавляет emit-шаг
            assert len(row) + extra == len(cols), f"{key}: {len(row)}+{extra} != {len(cols)}"

    # Покупка дошла до кампанийной грани с каналом SEM и кампанией из трекера.
    camp_rows = sets["campaign"]
    assert any(r[2] == "SEM" and r[3] == "direct:118498117" and r[4] == "men" for r in camp_rows)
    # Вход new/repeat: (dev, канал, кампании, разделы→[шт, деньги]).
    dev, ch, camps, by_sec = sets["nb_orders"][0]
    assert dev == "d1" and ch == "SEM" and camps == ("direct:118498117",)
    assert by_sec["men"][1] == 3000.0
    # DAU v1: устройство с карточкой + устройство со вкладкой.
    v1 = sets["daily"]
    assert any(r[2] == "men" and r[4] == 2 for r in v1), "dau men должен быть 2 (карточка+вкладка)"


class FakeAttrNone:
    """Устройство без атрибуции: last-touch не найден (прямой запуск)."""

    def publisher(self, dev, when):
        return "Без атрибуции"

    def campaign(self, dev, when):
        return None

    def source(self, dev, when):
        return "organic"


def test_build_app_day_unattributed_remainder():
    """Неатрибутированные устройства ложатся остатком campaign='' (канал Direct),
    а не выпадают из кампанийной грани — иначе бОльшая часть app-разделов
    не доезжает до Обзора вовсе."""
    with patch.object(A, "_stream_event", fake_stream):
        sets = A.build_app_day("2026-08-03", "tok", FakeResolver(), FakeAttrNone(), set())

    camp_rows = sets["campaign"]
    remainder = [r for r in camp_rows if r[3] == ""]
    assert remainder, "остаток campaign='' должен существовать"
    # Аудитория раздела (view_item d1) и покупка d1 — в остатке канала Direct.
    assert any(r[2] == "Direct" and r[4] == "men" and r[5] == 1 and r[8] == 1 for r in remainder), remainder
    # Тип-грань: покупка тоже в остатке.
    assert any(r[2] == "Direct" and r[3] == "" and r[4] == "men" for r in sets["campaign_type"])
    # nb_orders: канал остатка совпадает с каналом строки грани (иначе new-меры не лягут).
    dev, ch, camps, _by_sec = sets["nb_orders"][0]
    assert ch == "Direct" and camps == ("",)
