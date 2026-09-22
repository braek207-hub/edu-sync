"""Полная замена окна CRM не должна проходить по опустевшему источнику.

22.09.2026 Google-лист «Лиды» пересобирался (1 199 лидов вместо 112 941,
макс. дата 12.01 вместо 18.09), синк сделал DELETE date >= min и снёс
8 месяцев crm_leads/crm_lead_details. Гейт сравнивает новое с тем, что
уже лежит в том же окне, и отказывает, если стало заметно меньше.
"""

from datetime import date

import pytest

from sync.db import SourceShrunkError, check_not_shrunk


def test_empty_db_window_passes():
    check_not_shrunk("crm_leads", old_total=0, old_max=None, new_total=5, new_max=date(2026, 1, 3))


def test_growth_and_small_dip_pass():
    check_not_shrunk("crm_leads", old_total=100, old_max=date(2026, 9, 18), new_total=130, new_max=date(2026, 9, 19))
    # Чистка мусорных статусов, дедуп — законное уменьшение на проценты
    check_not_shrunk("crm_leads", old_total=100, old_max=date(2026, 9, 18), new_total=90, new_max=date(2026, 9, 16))


def test_total_halved_refused():
    with pytest.raises(SourceShrunkError, match="crm_leads"):
        check_not_shrunk("crm_leads", old_total=112_941, old_max=date(2026, 9, 18), new_total=1_199, new_max=date(2026, 9, 18))


def test_max_date_rolled_back_refused():
    # Лист заполнен по объёму, но заново и только до января: объём мог совпасть, дата — нет
    with pytest.raises(SourceShrunkError, match="2026-01-12"):
        check_not_shrunk("crm_leads", old_total=100, old_max=date(2026, 9, 18), new_total=100, new_max=date(2026, 1, 12))


def test_force_env_bypasses(monkeypatch):
    monkeypatch.setenv("CRM_REPLACE_FORCE", "1")
    check_not_shrunk("crm_leads", old_total=100, old_max=date(2026, 9, 18), new_total=1, new_max=date(2026, 1, 1))
