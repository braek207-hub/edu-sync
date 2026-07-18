# -*- coding: utf-8 -*-
"""Цели Директа читаются из config/lime_direct_goals.json.

Регрессия 47dc2b6 (2026-06-24): файловое чтение заменили на env LIME_DIRECT_GOALS
с плоским форматом на шесть ключей, где ступени install нет вовсе. Env в CI не задан,
поэтому _parse_goals возвращал ([], {}) → отчёт запрашивался без секции Goals →
conversions={} с середины июня. Мертвы были все 12 колонок целей в дашборде.
"""
from sync.lime_direct import (
    GOALS_PER_REPORT,
    _merge_report_chunks,
    _parse_goals,
    _pick_conversion_columns,
)

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


def test_goals_exceed_single_report_limit():
    """12 целей не влезают в один отчёт — Reports API принимает максимум 10.

    Именно на этот лимит напоролся переход на env: вместо порционных запросов
    список урезали до шести ключей и потеряли install.
    """
    goal_ids, _ = _parse_goals()
    assert len(goal_ids) > GOALS_PER_REPORT


def test_merge_report_chunks_unions_conversions():
    """Порции склеиваются по (date, campaign_id): conversions объединяются."""
    chunk_a = [{
        "date": "2026-07-01", "campaign_id": "1", "clicks": 10,
        "conversions": {"4": 5, "194380276": 2},
    }]
    chunk_b = [{
        "date": "2026-07-01", "campaign_id": "1", "clicks": 10,
        "conversions": {"3023504302": 7},
    }]
    merged = _merge_report_chunks([chunk_a, chunk_b])
    assert len(merged) == 1
    assert merged[0]["conversions"] == {"4": 5, "194380276": 2, "3023504302": 7}
    assert merged[0]["clicks"] == 10, "объёмные поля не должны суммироваться дважды"


def test_pick_conversion_columns_matches_lsccd_suffix():
    """Директ отдаёт _LSCCD на запрос LSC — захардкоженный суффикс обнулял все цели.

    Заголовки взяты из боевого прогона 2026-07-18 (run 29652806276).
    """
    header = [
        "Date", "CampaignId", "Clicks", "Cost",
        "Conversions_4_LSCCD", "Conversions_194380276_LSCCD",
    ]
    cols = _pick_conversion_columns(header, ["4", "194380276"], {"4": "install", "194380276": "cart"})
    assert cols == {"Conversions_4_LSCCD": "4", "Conversions_194380276_LSCCD": "194380276"}


def test_pick_conversion_columns_survives_model_change():
    """Смена модели атрибуции не должна снова обнулять цели."""
    header = ["Date", "Conversions_4_LC", "Conversions_9_SOMETHINGNEW"]
    cols = _pick_conversion_columns(header, ["4", "9"], {"4": "install", "9": "cart"})
    assert cols == {"Conversions_4_LC": "4", "Conversions_9_SOMETHINGNEW": "9"}


def test_pick_conversion_columns_reports_missing():
    header = ["Date", "Clicks"]
    cols = _pick_conversion_columns(header, ["4"], {"4": "install"})
    assert cols == {}


def test_merge_report_chunks_keeps_rows_missing_in_other_chunk():
    """Кампания, попавшая только в одну порцию, не теряется."""
    chunk_a = [{"date": "2026-07-01", "campaign_id": "1", "clicks": 10, "conversions": {"4": 5}}]
    chunk_b = [{"date": "2026-07-01", "campaign_id": "2", "clicks": 3, "conversions": {"4": 1}}]
    merged = _merge_report_chunks([chunk_a, chunk_b])
    assert {r["campaign_id"] for r in merged} == {"1", "2"}
