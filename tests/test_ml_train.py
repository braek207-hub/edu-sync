from datetime import date

from sync.ml_train import decide_gate, time_split


def test_gate_requires_beating_both():
    assert decide_gate(3.0, 2.0, 1.5) is True
    assert decide_gate(2.0, 2.0, 1.5) is False   # не строго больше baseline
    assert decide_gate(1.4, 1.0, 1.5) is False   # не бьёт pilot


def test_time_split_maturity_window():
    from datetime import date
    from sync.ml_train import time_split
    today = date(2026, 7, 23)
    rows = [
        {"created_date": date(2025, 6, 1), "client_id": "a"},   # age~418 → train
        {"created_date": date(2026, 5, 1), "client_id": "b"},   # age~83 <90 → исключён (молодой)
        {"created_date": date(2026, 3, 1), "client_id": "c"},   # age~144, в окне → test
    ]
    train, test = time_split(rows, holdout_months=3, today=today)
    assert [r["created_date"] for r in train] == [date(2025, 6, 1)]
    assert [r["created_date"] for r in test] == [date(2026, 3, 1)]


def test_time_split_drops_test_clients_in_train():
    from datetime import date
    from sync.ml_train import time_split
    today = date(2026, 7, 23)
    rows = [
        {"created_date": date(2025, 6, 1), "client_id": "dup"},   # train
        {"created_date": date(2026, 3, 1), "client_id": "dup"},   # test → выкинут (клиент в train)
        {"created_date": date(2026, 3, 2), "client_id": "solo"},  # test → остаётся
    ]
    train, test = time_split(rows, holdout_months=3, today=today)
    assert [r["client_id"] for r in test] == ["solo"]
