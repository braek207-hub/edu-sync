# -*- coding: utf-8 -*-
"""Цели Директа читаются из config/lime_direct_goals.json.

Регрессия 47dc2b6 (2026-06-24): файловое чтение заменили на env LIME_DIRECT_GOALS
с плоским форматом на шесть ключей, где ступени install нет вовсе. Env в CI не задан,
поэтому _parse_goals возвращал ([], {}) → отчёт запрашивался без секции Goals →
conversions={} с середины июня. Мертвы были все 12 колонок целей в дашборде.
"""
from sync.lime_direct import _parse_goals

# Зеркало config/lime-direct-goals.json дашборда. Синк и дашборд обязаны сходиться
# по id: расхождение обнулит цели молча, без ошибки.
EXPECTED_IDS = {
    "4",           # App installs (app_ios, app_android)
    "38403071",    # Added to cart (AppMetrica)
    "38403173",    # Made a purchase (AppMetrica)
    "194380276",   # Ecommerce: добавление в корзину (web)
    "340817822",   # Ecommerce: начало оформления (web)
    "1900016997",  # ECOMMERCE_ADD_TO_CART (ios)
    "1900016998",  # ECOMMERCE_ADD_TO_CART (android)
    "1900016999",  # ECOMMERCE_PURCHASE (ios)
    "1900017000",  # ECOMMERCE_PURCHASE (android)
    "1900025332",  # begin_checkout (ios)
    "1900025333",  # begin_checkout (android)
    "3023504302",  # Ecommerce: покупка (web)
}


def test_parse_goals_reads_all_twelve_from_config():
    goal_ids, id_to_step = _parse_goals()
    assert len(goal_ids) == 12
    assert set(goal_ids) == EXPECTED_IDS


def test_parse_goals_keeps_install_step():
    """Ступень install терялась первой: в env-формате её не было вовсе."""
    _, id_to_step = _parse_goals()
    assert id_to_step["4"] == "install"


def test_parse_goals_covers_all_four_steps():
    _, id_to_step = _parse_goals()
    assert set(id_to_step.values()) == {"install", "cart", "checkout", "purchase"}


def test_parse_goals_ignores_env(monkeypatch):
    """Источник истины — файл. Env не должен ни переопределять, ни обнулять цели."""
    monkeypatch.setenv("LIME_DIRECT_GOALS", "")
    goal_ids, _ = _parse_goals()
    assert len(goal_ids) == 12
