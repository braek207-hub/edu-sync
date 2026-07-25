from datetime import date

import numpy as np

from sync.ml_score import to_deciles, is_pending, expected_amounts


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


class _FakeVec:
    def transform(self, X):
        return np.array([[row["a"]] for row in X])


class _FakeTweedieModel:
    def predict(self, Xt):
        return Xt[:, 0] * 100.0   # E(amount) линейно от фичи "a"


def test_expected_amounts_uses_tweedie_over_full_matrix():
    tw = {"model": _FakeTweedieModel(), "vec": _FakeVec()}
    X = [{"a": 1.0}, {"a": 2.0}, {"a": 3.0}]
    out = expected_amounts(tw, X)
    assert list(out) == [100.0, 200.0, 300.0]   # каждый лид точки, не только pending


def test_expected_amounts_zero_when_no_tweedie_model():
    X = [{"a": 1.0}, {"a": 2.0}]
    out = expected_amounts({"model": None, "vec": None}, X)
    assert list(out) == [0.0, 0.0]
    assert expected_amounts(None, X).tolist() == [0.0, 0.0]
