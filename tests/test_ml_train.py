from datetime import date

from sync.ml_train import decide_gate, time_split


def test_gate_beats_pilot():
    assert decide_gate(0.067, 0.037) is True     # AP модели строго бьёт пилота
    assert decide_gate(0.037, 0.037) is False    # не строго больше пилота
    assert decide_gate(0.030, 0.037) is False    # хуже пилота


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


def _synthetic_rows():
    """Созревшие train (старые) + holdout (в окне). Сигнал — в beh_page_depth (его
    пилот НЕ использует), а пилот-фичи (duration/visits/bounce) держим константными →
    логистика бьёт пилота (иначе ничья и gate=False). Клиенты не пересекаются (анти-утечка)."""
    def _row(cd, cid, lid, paid):
        return {
            "created_date": cd, "client_id": cid, "lead_id": lid,
            "is_matured": True, "label_connected": True, "label_deal": paid,
            "label_paid": paid, "amount": 1000.0 if paid else 0.0, "direction": "vuz",
            "features": {
                "f__beh_page_depth": 10.0 if paid else 1.0,   # сигнал (пилот его не видит)
                "f__beh_avg_duration_sec": 100.0,             # пилот-фичи константны
                "f__beh_visits": 1.0, "f__beh_bounce_rate": 50.0,
            },
        }
    rows = [_row(date(2025, 6, 1), f"tr{i}", f"L_tr{i}", i >= 10) for i in range(20)]
    rows += [_row(date(2026, 3, 1), f"te{i}", f"L_te{i}", i >= 5) for i in range(10)]
    return rows


def test_train_one_point_local_smoke():
    """Локальный smoke прод-пути (sklearn, без catboost/БД): логистика на сепарабельной
    синтетике → run с prauc_pay, gate_passed bool, артефакт 'logistic'."""
    from sync.ml_train import _train_one_point
    res = _train_one_point(_synthetic_rows(), "at_creation", date(2026, 7, 23))
    run, artifacts = res["run"], res["artifacts"]
    assert isinstance(run["prauc_pay"], float)
    assert run["prauc_pay"] > 0.9                 # сигнал сепарабельный
    assert isinstance(run["gate_passed"], bool)
    assert run["gate_passed"] is True             # модель бьёт пилота
    assert run["stage_metrics"]["model"] == "logistic_single_stage"
    assert "logistic" in artifacts and "tweedie" in artifacts and "manifest" in artifacts
