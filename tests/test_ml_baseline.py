import numpy as np
from sync.ml.baseline import pilot_score, fit_logistic_baseline

def test_pilot_score_monotone_in_duration():
    low = [{"f__beh_avg_duration_sec": 50, "f__beh_visits": 1, "f__beh_bounce_rate": 80}]
    high = [{"f__beh_avg_duration_sec": 300, "f__beh_visits": 5, "f__beh_bounce_rate": 5}]
    assert pilot_score(high)[0] > pilot_score(low)[0]
    s = pilot_score(low + high)
    assert np.all((s >= 0) & (s <= 1))

def test_logistic_baseline_learns_signal():
    rows = [{"x": float(i)} for i in range(20)]
    y = [0]*10 + [1]*10
    pred = fit_logistic_baseline(rows, cat_names=[], y=y)
    p = pred(rows)
    assert p[-1] > p[0]   # больший x → выше вероятность
