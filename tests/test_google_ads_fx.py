# -*- coding: utf-8 -*-
"""Конвертация расхода Google Ads в рубли.

Шаг добавлен в workflow 2026-07-18 (c742832) с окном 30 дней, поэтому история
с июня 2025 осталась без cost_rub — а хендлер дашборда читает именно его, и расход
Google Ads показывался нулевым за всю историю.

Режим дозаполнения берёт строки по cost_rub IS NULL без оконного ограничения:
разово закрывает историю и самозалечивается, если дневной шаг не отработает.
"""
from sync.google_ads_fx import build_pairs_query


def test_backfill_mode_targets_nulls_and_ignores_window():
    sql, params = build_pairs_query(backfill=True, frm="2026-06-18", to="2026-07-18")
    assert "cost_rub IS NULL" in sql
    assert params == ()


def test_window_mode_keeps_date_range():
    sql, params = build_pairs_query(backfill=False, frm="2026-06-18", to="2026-07-18")
    assert "cost_rub IS NULL" not in sql
    assert params == ("2026-06-18", "2026-07-18")


def test_both_modes_select_date_and_currency():
    """Обе ветки обязаны отдавать одинаковую форму строк — вызывающий код общий."""
    for backfill in (True, False):
        sql, _ = build_pairs_query(backfill=backfill, frm="2026-01-01", to="2026-01-31")
        assert "date::text AS date" in sql
        assert "currency" in sql
        assert "lime_google_ads_stats" in sql
