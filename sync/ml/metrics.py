"""Метрики качества скоринга. Чистые функции на numpy."""

import numpy as np


def lift_at_decile(y_true, scores, decile: int = 1) -> float:
    y = np.asarray(y_true, dtype=float)
    s = np.asarray(scores, dtype=float)
    n = len(y)
    if n == 0:
        return 0.0
    base = y.mean()
    if base <= 0:
        return 0.0
    k = max(1, int(round(n * 0.1 * decile)))
    order = np.argsort(-s)
    top_rate = y[order[:k]].mean()
    return float(top_rate / base)


def brier(y_true, p) -> float:
    y = np.asarray(y_true, dtype=float)
    pp = np.asarray(p, dtype=float)
    if len(y) == 0:
        return 0.0
    return float(np.mean((pp - y) ** 2))
