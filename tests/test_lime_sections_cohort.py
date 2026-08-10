# -*- coding: utf-8 -*-
"""Атрибуция когорты разделов LIME: чистая функция attribute_day.

БД-обвязка (sync_cohort_web) юнитами не покрывается — её сверяет санити-чек
плана: сумма когортных покупок закрытого окна ≤ gross.
"""
from sync.lime_sections_cohort import COHORT_WINDOW_DAYS, attribute_day

CLICK = ("direct:123", "SEM")


def test_same_day_click_and_purchase_is_d0():
    cells, clicks, first = attribute_day(
        "2026-08-01",
        clicks={1: CLICK},
        orders=[(1, {"women": [2.0, 5000.0]})],
        state={},
    )
    assert cells[("2026-08-01", "direct:123", "SEM", "women")] == [1, 1, 2.0, 5000.0]
    assert clicks == {CLICK: 1}
    assert first == {1}


def test_purchase_attributed_to_click_within_window():
    state = {1: ("2026-07-15", "direct:9", "SEM", None)}
    cells, _, first = attribute_day("2026-08-01", {}, [(1, {"men": [1.0, 3000.0]})], state)
    assert cells[("2026-07-15", "direct:9", "SEM", "men")] == [1, 1, 1.0, 3000.0]
    assert first == {1}


def test_purchase_outside_window_dropped():
    old = ("2026-06-01", "direct:9", "SEM", None)   # старше 30 дней
    cells, _, first = attribute_day("2026-08-01", {}, [(1, {"men": [1.0, 100.0]})], {1: old})
    assert cells == {} and first == set()


def test_window_boundary_inclusive():
    from datetime import date, timedelta
    click_day = (date(2026, 8, 1) - timedelta(days=COHORT_WINDOW_DAYS)).isoformat()
    cells, _, _ = attribute_day("2026-08-01", {}, [(1, {"men": [1.0, 100.0]})],
                                {1: (click_day, "vk:x", "SMM paid", None)})
    assert len(cells) == 1


def test_new_click_resets_window_and_wins_over_state():
    # Клиент кликнул месяц назад и снова сегодня: покупка уходит СЕГОДНЯШНЕЙ кампании.
    state = {1: ("2026-07-25", "direct:old", "SEM", "2026-07-26")}
    cells, _, first = attribute_day(
        "2026-08-01", {1: ("vk:new", "SMM paid")}, [(1, {"kids": [1.0, 900.0]})], state)
    assert list(cells) == [("2026-08-01", "vk:new", "SMM paid", "kids")]
    # Новый клик открывает новое окно — покупка снова «первая».
    assert first == {1}


def test_repeat_run_counts_same_buyers():
    # Повторный прогон дня: в БД first_buy_date уже = D — buyers не должны обнулиться.
    state = {1: ("2026-07-20", "direct:9", "SEM", "2026-08-01")}
    cells, _, first = attribute_day("2026-08-01", {}, [(1, {"men": [1.0, 100.0]})], state)
    assert cells[("2026-07-20", "direct:9", "SEM", "men")][0] == 1
    assert first == {1}


def test_second_purchase_same_day_not_first():
    state = {1: ("2026-07-20", "direct:9", "SEM", None)}
    cells, _, first = attribute_day(
        "2026-08-01", {},
        [(1, {"men": [1.0, 100.0]}), (1, {"men": [1.0, 200.0]})], state)
    # Заказов два, покупатель один.
    assert cells[("2026-07-20", "direct:9", "SEM", "men")] == [1, 2, 2.0, 300.0]
    assert first == {1}


def test_earlier_first_buy_makes_later_not_first():
    # Первая покупка окна была 25.07 — сегодняшняя уже повторная.
    state = {1: ("2026-07-20", "direct:9", "SEM", "2026-07-25")}
    cells, _, first = attribute_day("2026-08-01", {}, [(1, {"men": [1.0, 100.0]})], state)
    assert cells[("2026-07-20", "direct:9", "SEM", "men")] == [0, 1, 1.0, 100.0]
    assert first == set()


def test_purchase_without_click_ignored_but_clicks_counted():
    cells, clicks, _ = attribute_day(
        "2026-08-01", {2: CLICK}, [(1, {"men": [1.0, 100.0]})], {})
    assert cells == {}
    assert clicks == {CLICK: 1}


def test_order_with_two_sections_one_buyer_per_section():
    cells, _, first = attribute_day(
        "2026-08-01", {1: CLICK}, [(1, {"men": [1.0, 100.0], "women": [1.0, 200.0]})], {})
    assert cells[("2026-08-01", "direct:123", "SEM", "men")] == [1, 1, 1.0, 100.0]
    assert cells[("2026-08-01", "direct:123", "SEM", "women")] == [1, 1, 1.0, 200.0]
    assert first == {1}
