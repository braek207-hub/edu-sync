# -*- coding: utf-8 -*-
"""tests/test_vat.py — ставка НДС по дате дня: 20% до 2026, 22% с 01.01.2026."""

from datetime import date, datetime

import pytest

from sync.vat import facts_vat_rate, vat_rate, window_vat_rate


def test_rate_on_both_sides_of_the_boundary():
    assert vat_rate(date(2025, 1, 1)) == 1.2
    assert vat_rate(date(2025, 12, 31)) == 1.2
    assert vat_rate(date(2026, 1, 1)) == 1.22
    assert vat_rate(date(2026, 10, 24)) == 1.22


def test_datetime_is_not_a_day():
    with pytest.raises(TypeError):
        vat_rate(datetime(2026, 1, 1))


def test_window_inside_one_year():
    assert window_vat_rate(date(2025, 12, 28), 7) == pytest.approx(1.2)
    assert window_vat_rate(date(2026, 1, 11), 7) == pytest.approx(1.22)


def test_week_across_new_year_takes_each_day():
    # Пн 29.12.2025 – Вс 04.01.2026: 3 дня по 1.20 + 4 дня по 1.22.
    assert window_vat_rate(date(2026, 1, 4), 7) == pytest.approx((3 * 1.2 + 4 * 1.22) / 7)


def test_window_rejects_empty():
    with pytest.raises(ValueError):
        window_vat_rate(date(2026, 1, 1), 0)


def test_facts_window_ends_yesterday():
    # Сегодня 01.01.2026 → окно 28 дней до 31.12.2025 целиком в 2025.
    assert facts_vat_rate(date(2026, 1, 1)) == pytest.approx(1.2)
    # 29.01.2026 → окно 01.01–28.01.2026 целиком в 2026.
    assert facts_vat_rate(date(2026, 1, 29)) == pytest.approx(1.22)
    # 15.01.2026 → 14 дней 2025 (18–31.12) + 14 дней 2026 (01–14.01).
    assert facts_vat_rate(date(2026, 1, 15)) == pytest.approx((14 * 1.2 + 14 * 1.22) / 28)
