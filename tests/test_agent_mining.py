from sync.agent.mining import detect_change_points, did_effect, mine_quasi_experiments


def test_detects_step_change():
    series = [{"date": f"2026-06-{d:02d}", "value": 1000.0} for d in range(1, 15)]
    series += [{"date": f"2026-06-{d:02d}", "value": 2000.0} for d in range(15, 29)]
    points = detect_change_points(series, min_jump=0.3)
    assert len(points) == 1
    assert points[0]["date"] == "2026-06-15"


def test_ignores_noise_below_threshold():
    series = [{"date": f"2026-06-{d:02d}", "value": 1000.0 + (d % 3) * 10} for d in range(1, 29)]
    assert detect_change_points(series, min_jump=0.3) == []


def test_ignores_change_without_enough_history():
    series = [{"date": "2026-06-01", "value": 100.0}, {"date": "2026-06-02", "value": 900.0}]
    assert detect_change_points(series, min_jump=0.3) == []


def test_did_subtracts_control_movement():
    # Обработанная улучшилась на 20%, контроль — на 5%. Заслуга = 15 п.п.
    out = did_effect(treated_before=100.0, treated_after=80.0,
                     control_before=100.0, control_after=95.0)
    assert abs(out["effect"] - (-0.15)) < 1e-9


def test_did_returns_none_on_zero_base():
    out = did_effect(0.0, 80.0, 100.0, 95.0)
    assert out["effect"] is None


def test_did_confidence_interval_is_wide_for_quasi():
    out = did_effect(100.0, 80.0, 100.0, 95.0)
    assert out["effect_lo"] < out["effect"] < out["effect_hi"]


def test_mine_emits_class_b_rows():
    facts = []
    for d in range(1, 29):
        for cid, cost in (("111", 1000.0 if d < 15 else 2000.0), ("222", 1000.0)):
            facts.append({
                "fact_date": f"2026-06-{d:02d}",
                "campaign_id": cid,
                "cost": cost,
                "sum_p_pay": 10.0,
            })
    rows = mine_quasi_experiments(facts, window=7)
    assert rows, "изменение бюджета кампании 111 должно быть найдено"
    assert all(r["reliability_class"] == "B" for r in rows)
    assert all(r["source"] == "quasi" for r in rows)
    assert all(r["mechanism"] == "did" for r in rows)
    assert all(r["experiment_id"] for r in rows)


def test_mine_returns_empty_on_flat_history():
    facts = [{"fact_date": f"2026-06-{d:02d}", "campaign_id": "111", "cost": 1000.0, "sum_p_pay": 10.0}
             for d in range(1, 29)]
    assert mine_quasi_experiments(facts, window=7) == []
