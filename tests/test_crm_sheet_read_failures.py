# -*- coding: utf-8 -*-
"""Ошибка чтения листа CRM фатальна, а не «пропуск».

Прогон 32613839112 (23.08): единичный 503 на листе «Оплаты» был молча
проглочен, синк продолжил с пустым словарём оплат и переписал crm_lead_details
с is_paid=false у всех лидов 2026 года. Эти тесты закрепляют обратное
поведение: чтение упало — упал и синк, данные не трогаются.
"""

import pytest

from sync import crm


def _boom(*args, **kwargs):
    raise RuntimeError("HttpError 503: The service is currently unavailable.")


def test_paid_by_lead_id_propagates_sheet_read_error(monkeypatch):
    monkeypatch.setattr(crm, "read_sheet", _boom)
    with pytest.raises(RuntimeError):
        crm._load_paid_by_lead_id(object(), "sheet-id")


def test_lead_dims_propagate_sheet_read_error(monkeypatch):
    monkeypatch.setattr(crm, "read_sheet", _boom)
    with pytest.raises(RuntimeError):
        crm._load_all_lead_dims(object(), "sheet-id")


# 17.11.2026: строку заголовков листа «Оплаты» затёрла строка оплаты — колонки
# «ID лида» не стало, синк «пропустил» лист и проставил is_paid=false всем лидам.
# Потерянный заголовок или пустой лист оплат — тоже фатально.

_PAY_ROW = ["22873560", "11078137", "39173375", "12.11.2026 22:46", "Веб-сайт", "yandex_s", "vuz", "712704859"]


def test_paid_by_lead_id_fails_without_lead_column(monkeypatch):
    monkeypatch.setattr(crm, "read_sheet", lambda *a, **k: [_PAY_ROW + ["Выручка", "orders", "date pay"], _PAY_ROW])
    with pytest.raises(RuntimeError, match="ID лида"):
        crm._load_paid_by_lead_id(object(), "sheet-id")


def test_paid_by_lead_id_fails_on_empty_sheet(monkeypatch):
    monkeypatch.setattr(crm, "read_sheet", lambda *a, **k: [["ID лида в SCRM", "orders"]])
    with pytest.raises(RuntimeError, match="пуст"):
        crm._load_paid_by_lead_id(object(), "sheet-id")


def test_payments_sync_fails_without_campaign_column(monkeypatch):
    monkeypatch.setattr(crm, "get_sheets_service", lambda: object())
    monkeypatch.setenv("GOOGLE_SHEETS_ID", "sheet-id")
    monkeypatch.setattr(crm, "_load_all_lead_dims", lambda *a: {})
    monkeypatch.setattr(crm, "read_sheet", lambda *a, **k: [_PAY_ROW + ["Выручка", "orders", "date pay"], _PAY_ROW])
    with pytest.raises(RuntimeError, match="Оплаты"):
        crm.sync_crm_payments()
