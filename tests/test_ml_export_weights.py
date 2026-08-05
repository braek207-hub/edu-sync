"""Тесты экспорта весов логистики (реалтайм-скоринг Vercel /api/edu/lead-quality).
Ключевой инвариант: линейная формула по экспортированным весам воспроизводит
sklearn predict_proba бит-в-бит (модель без скейлера), включая молчаливый дроп
невиданных категорий DictVectorizer'ом."""

import json

import numpy as np

from sync.ml.baseline import fit_logistic, predict_logistic
from sync.ml_export_weights import (
    build_payload,
    extract_weights,
    pick_samples,
    quality_threshold,
)
from sync.ml_score import to_deciles


def _toy_model():
    rows = [{"num": float(i), "cat": "a" if i % 2 else "b"} for i in range(40)]
    y = [1 if i > 30 else 0 for i in range(40)]
    clf, vec = fit_logistic(rows, y)
    return clf, vec, rows


def _manual_p(intercept: float, coefs: dict, row: dict) -> float:
    z = intercept
    for name, val in row.items():
        if isinstance(val, str):
            z += coefs.get(f"{name}={val}", 0.0)
        else:
            z += coefs.get(name, 0.0) * float(val)
    return float(1.0 / (1.0 + np.exp(-z)))


def test_extract_weights_reproduces_sklearn():
    clf, vec, rows = _toy_model()
    intercept, coefs = extract_weights(clf, vec)
    p_expected = predict_logistic(clf, vec, rows)
    for row, pe in zip(rows, p_expected):
        assert abs(_manual_p(intercept, coefs, row) - pe) < 1e-9


def test_unseen_category_contributes_zero():
    """vec.transform молча дропает невиданное значение — формула с coefs.get(..., 0)
    обязана совпасть (это же поведение повторяет TS-скоринг на Vercel)."""
    clf, vec, _ = _toy_model()
    intercept, coefs = extract_weights(clf, vec)
    row = {"num": 3.0, "cat": "NEVER_SEEN"}
    pe = predict_logistic(clf, vec, [row])[0]
    assert abs(_manual_p(intercept, coefs, row) - pe) < 1e-9


def test_quality_threshold_matches_rank_deciles():
    rng = np.random.RandomState(7)
    p = rng.rand(997)
    thr = quality_threshold(p)
    for pi, di in zip(p, to_deciles(p)):
        assert (pi >= thr) == (di <= 2)


def test_pick_samples_even_and_deterministic():
    pop = [{"lead_id": f"L{i:03d}"} for i in range(100)]
    X = [{"num": float(i)} for i in range(100)]
    p = [i / 100 for i in range(100)]
    s1 = pick_samples(pop, X, p)
    assert s1 == pick_samples(pop, X, p)
    assert len(s1) == 20
    assert s1[0]["lead_id"] == "L000" and s1[1]["lead_id"] == "L005"


def test_payload_json_serializable():
    payload = build_payload(
        "20260805", -3.5, {"num": 0.1, "cat=a": -0.2}, 0.42,
        ["2026-08-20"], [{"lead_id": "L1", "features": {"num": 1.0}, "p_raw": 0.5}], 100,
    )
    parsed = json.loads(json.dumps(payload, ensure_ascii=False))
    assert parsed["quality_threshold_p_raw"] == 0.42
    assert parsed["quality_max_decile"] == 2
    assert parsed["admission_calendar"] == ["2026-08-20"]
