from sync.ml.forecast import expected_revenue, aggregate_forecast

def test_expected_revenue():
    assert abs(expected_revenue(0.2, 100000, 0.5) - 10000.0) < 1e-6

def test_aggregate_segments_and_all():
    items = [
        {"segment": "it", "exp_rev": 1000.0, "p_pay": 0.2},
        {"segment": "it", "exp_rev": 2000.0, "p_pay": 0.3},
        {"segment": "med", "exp_rev": 500.0, "p_pay": 0.1},
    ]
    out = {r["segment"]: r for r in aggregate_forecast(items)}
    assert out["it"]["pending_leads"] == 2
    assert abs(out["it"]["exp_revenue"] - 3000.0) < 1e-6
    assert abs(out["it"]["exp_payments"] - 0.5) < 1e-6
    assert out["all"]["pending_leads"] == 3
    assert abs(out["all"]["exp_revenue"] - 3500.0) < 1e-6
    assert out["all"]["revenue_lo"] <= out["all"]["exp_revenue"] <= out["all"]["revenue_hi"]
    assert out["all"]["revenue_lo"] >= 0.0
