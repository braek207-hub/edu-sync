"""Бейзлайны для гейта: эвристика пилота (время визита) + логистическая регрессия."""

import numpy as np
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import LogisticRegression


def pilot_score(feature_dicts) -> np.ndarray:
    """Порт эвристики пилота: нормированное время визита + визиты − отказы.
    Веса грубые (пилот: оплатившие 186с vs 116с); используется только как планка гейта."""
    out = []
    for fd in feature_dicts:
        dur = float(fd.get("f__beh_avg_duration_sec") or 0.0)
        vis = float(fd.get("f__beh_visits") or 0.0)
        bounce = float(fd.get("f__beh_bounce_rate") or 0.0)
        dur_n = min(dur / 300.0, 1.0)          # 300с ≈ насыщение
        vis_n = min(vis / 5.0, 1.0)
        bounce_n = min(bounce / 100.0, 1.0)
        score = 0.6 * dur_n + 0.3 * vis_n + 0.1 * (1.0 - bounce_n)
        out.append(score)
    return np.clip(np.asarray(out, dtype=float), 0.0, 1.0)


def fit_logistic_baseline(rows, cat_names, y):
    vec = DictVectorizer(sparse=False)
    X = vec.fit_transform(rows)
    clf = LogisticRegression(max_iter=1000, class_weight="balanced")
    clf.fit(X, np.asarray(y, dtype=int))

    def predict_proba1(rows2) -> np.ndarray:
        X2 = vec.transform(rows2)
        return clf.predict_proba(X2)[:, 1]

    return predict_proba1
