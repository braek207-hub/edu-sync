# -*- coding: utf-8 -*-
"""Помесячная выборка Метрики (лечение 429 на бэкфилле).

Синк дёргал Stat API на КАЖДЫЙ день, а на день уходит пять запросов — 3285 обращений
на полную историю. Метрика отвечала 429 и не отпускала даже после пяти ретраев с паузой.
Диапазон допустим (ym:s:date стоит в измерениях), поэтому тянем помесячно: ~110 запросов.
"""
from datetime import date

from sync.lime_gcc import _month_spans


def test_single_day():
    assert _month_spans(date(2026, 7, 19), date(2026, 7, 19)) == [
        (date(2026, 7, 19), date(2026, 7, 19))
    ]


def test_within_one_month():
    assert _month_spans(date(2026, 7, 3), date(2026, 7, 20)) == [
        (date(2026, 7, 3), date(2026, 7, 20))
    ]


def test_splits_on_month_boundary():
    assert _month_spans(date(2026, 6, 28), date(2026, 7, 2)) == [
        (date(2026, 6, 28), date(2026, 6, 30)),
        (date(2026, 7, 1), date(2026, 7, 2)),
    ]


def test_crosses_new_year():
    """Декабрь → январь: месяц+1 переполняется, год обязан вырасти."""
    assert _month_spans(date(2025, 12, 30), date(2026, 1, 2)) == [
        (date(2025, 12, 30), date(2025, 12, 31)),
        (date(2026, 1, 1), date(2026, 1, 2)),
    ]


def test_february_leap_year():
    spans = _month_spans(date(2028, 2, 1), date(2028, 3, 1))
    assert spans[0] == (date(2028, 2, 1), date(2028, 2, 29))


def test_february_non_leap():
    spans = _month_spans(date(2026, 2, 1), date(2026, 3, 1))
    assert spans[0] == (date(2026, 2, 1), date(2026, 2, 28))


def test_full_history_is_continuous_and_complete():
    """Ни одного пропущенного и ни одного задвоенного дня на всей истории GCC."""
    frm, to = date(2024, 10, 1), date(2026, 7, 19)
    spans = _month_spans(frm, to)

    assert spans[0][0] == frm and spans[-1][1] == to
    for (_, prev_end), (next_start, _) in zip(spans, spans[1:]):
        assert (next_start - prev_end).days == 1, "разрыв или нахлёст между месяцами"

    covered = sum((end - start).days + 1 for start, end in spans)
    assert covered == (to - frm).days + 1


def test_requests_saved_vs_per_day():
    """Смысл правки: на порядок меньше обращений к API."""
    spans = _month_spans(date(2024, 10, 1), date(2026, 7, 19))
    days = (date(2026, 7, 19) - date(2024, 10, 1)).days + 1
    assert len(spans) * 5 < days * 5 / 20
