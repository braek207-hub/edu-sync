from datetime import date

from sync.ml_score import to_deciles, is_pending


def test_deciles_top_is_one():
    d = to_deciles([0.9, 0.1, 0.5, 0.3])
    assert d[0] == 1                 # наибольший скор → дециль 1
    assert min(d) == 1 and max(d) <= 10


def test_is_pending():
    young_unpaid = {"label_paid": None, "created_date": date(2026, 7, 20)}
    old_unpaid = {"label_paid": False, "created_date": date(2025, 1, 1)}
    paid = {"label_paid": True, "created_date": date(2026, 7, 20)}
    assert is_pending(young_unpaid, today=date(2026, 7, 23)) is True
    assert is_pending(old_unpaid, today=date(2026, 7, 23)) is False   # созрел, не оплатил
    assert is_pending(paid, today=date(2026, 7, 23)) is False
