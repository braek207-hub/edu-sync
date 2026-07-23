from datetime import date

from sync.ml_train import decide_gate, time_split


def test_gate_requires_beating_both():
    assert decide_gate(3.0, 2.0, 1.5) is True
    assert decide_gate(2.0, 2.0, 1.5) is False   # не строго больше baseline
    assert decide_gate(1.4, 1.0, 1.5) is False   # не бьёт pilot


def test_time_split_holdout():
    rows = [{"created_date": date(2026, 1, 1)}, {"created_date": date(2026, 7, 20)}]
    train, test = time_split(rows, holdout_months=3, today=date(2026, 7, 23))
    assert len(train) == 1 and train[0]["created_date"] == date(2026, 1, 1)
    assert len(test) == 1 and test[0]["created_date"] == date(2026, 7, 20)
