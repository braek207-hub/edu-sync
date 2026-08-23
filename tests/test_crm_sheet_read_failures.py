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
