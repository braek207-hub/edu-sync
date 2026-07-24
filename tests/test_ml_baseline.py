import numpy as np
from sync.ml.baseline import (
    pilot_score,
    fit_logistic,
    predict_logistic,
    logistic_top_factors,
)


def test_pilot_score_monotone_in_duration():
    low = [{"f__beh_avg_duration_sec": 50, "f__beh_visits": 1, "f__beh_bounce_rate": 80}]
    high = [{"f__beh_avg_duration_sec": 300, "f__beh_visits": 5, "f__beh_bounce_rate": 5}]
    assert pilot_score(high)[0] > pilot_score(low)[0]
    s = pilot_score(low + high)
    assert np.all((s >= 0) & (s <= 1))


def test_logistic_fit_predict_separable():
    """Сепарабельный toy-датасет → AP≈1.0 (реальный локальный прогон sklearn)."""
    from sklearn.metrics import average_precision_score
    rows = [{"x": float(i)} for i in range(20)]
    y = [0] * 10 + [1] * 10
    clf, vec = fit_logistic(rows, y)
    p = predict_logistic(clf, vec, rows)
    assert p[-1] > p[0]                       # больший x → выше вероятность
    assert average_precision_score(y, p) > 0.99


def test_logistic_serializable():
    """(clf, vec) должны пиклиться (в отличие от замыкания)."""
    import pickle
    rows = [{"x": float(i)} for i in range(20)]
    y = [0] * 10 + [1] * 10
    clf, vec = fit_logistic(rows, y)
    clf2, vec2 = pickle.loads(pickle.dumps((clf, vec)))
    p1 = predict_logistic(clf, vec, rows)
    p2 = predict_logistic(clf2, vec2, rows)
    assert np.allclose(p1, p2)


def test_logistic_top_factors_shape_and_sort():
    rows = [{"x": float(i), "z": float(i % 2)} for i in range(20)]
    y = [0] * 10 + [1] * 10
    clf, vec = fit_logistic(rows, y)
    top = logistic_top_factors(clf, vec, rows[-1], k=3)
    assert 1 <= len(top) <= 3
    for d in top:
        assert set(d.keys()) == {"feature", "shap"}
        assert isinstance(d["feature"], str)
        assert isinstance(d["shap"], float)
    mags = [abs(d["shap"]) for d in top]
    assert mags == sorted(mags, reverse=True)   # отсортировано по |вкладу|
