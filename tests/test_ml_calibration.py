"""Ф2.2: изотоническая калибровка поверх логистики. Монотонность → ранжирование/
дециль/AP тождественны; калибровка стягивает p_pay к истинной базовой ставке
(class_weight="balanced" инфлирует сырые p) → прогноз выручки становится корректным."""

import numpy as np

from sync.ml.artifacts import deserialize_pickle, serialize_pickle
from sync.ml.baseline import fit_logistic, predict_logistic


class _NoopTweedieVec:
    """Заглушка Tweedie-вектора для тестов _score_point — модуль-level (пиклится),
    _score_point не вызывает tw.predict сам (только сохраняет для прогноза выручки)."""

    def predict(self, X):
        return np.zeros(len(X))


def test_isotonic_calibration_preserves_rank_order():
    """Изотоника монотонна → argsort(p_cal) == argsort(p_raw) → дециль/AP не меняются."""
    from sklearn.isotonic import IsotonicRegression

    rows = [{"x": float(i)} for i in range(40)]
    y = [0] * 20 + [1] * 20
    clf, vec = fit_logistic(rows, y)
    p_raw = predict_logistic(clf, vec, rows)
    calibrator = IsotonicRegression(out_of_bounds="clip").fit(p_raw, y)
    p_cal = calibrator.predict(p_raw)

    assert np.array_equal(np.argsort(p_raw), np.argsort(p_cal))
    # монотонность и напрямую: по возрастанию p_raw p_cal никогда не убывает
    order = np.argsort(p_raw)
    assert np.all(np.diff(p_cal[order]) >= -1e-12)


def test_isotonic_calibration_pulls_average_to_base_rate():
    """На синтетике с известной базовой ставкой (редкий позитив) balanced-логистика
    инфлирует avg(p_raw) далеко от base_rate; изотоника (in-sample train) стягивает
    avg(p_cal) обратно к истинной базе."""
    from sklearn.isotonic import IsotonicRegression
    from sklearn.linear_model import LogisticRegression

    rng = np.random.RandomState(0)
    n = 2000
    X = rng.randn(n, 3)
    logit = 0.8 * X[:, 0] + 0.3 * X[:, 1] - 0.1 * X[:, 2] - 4.0   # редкий позитив
    prob = 1 / (1 + np.exp(-logit))
    y = (rng.rand(n) < prob).astype(int)
    base_rate = y.mean()
    assert base_rate < 0.06                      # действительно редкий позитив

    clf = LogisticRegression(max_iter=1000, class_weight="balanced")
    clf.fit(X, y)
    p_raw = clf.predict_proba(X)[:, 1]
    avg_p_raw = p_raw.mean()

    calibrator = IsotonicRegression(out_of_bounds="clip").fit(p_raw, y)
    p_cal = calibrator.predict(p_raw)
    avg_p_cal = p_cal.mean()

    assert avg_p_raw > base_rate * 3              # balanced инфлирует сильно (как в проде: 0.26 vs 0.014)
    assert abs(avg_p_cal - base_rate) < 0.01       # калибровка стягивает почти точно к базе (in-sample)
    assert avg_p_cal < avg_p_raw


def test_calibrator_pickle_roundtrip():
    """Калибратор (IsotonicRegression) должен сериализоваться/десериализоваться,
    как и (clf, vec) — иначе не переживёт запись в edu_ml_artifacts (bytea)."""
    from sklearn.isotonic import IsotonicRegression

    rows = [{"x": float(i)} for i in range(20)]
    y = [0] * 10 + [1] * 10
    clf, vec = fit_logistic(rows, y)
    p_raw = predict_logistic(clf, vec, rows)
    calibrator = IsotonicRegression(out_of_bounds="clip").fit(p_raw, y)

    blob = serialize_pickle(calibrator)
    restored = deserialize_pickle(blob)
    assert np.allclose(restored.predict(p_raw), calibrator.predict(p_raw))


def test_score_point_applies_calibration_to_p_pay(monkeypatch):
    """_score_point: p_pay/p_final должны быть КАЛИБРОВАННЫМИ (не сырыми p из логистики).
    БД замокана (никаких реальных соединений) — только db.load_latest_passing_artifacts
    и db.upsert_lead_scores, как их видит sync.ml_score."""
    from sklearn.isotonic import IsotonicRegression

    from sync import db
    from sync.ml_score import _score_point

    rows_train = [{"x": float(i)} for i in range(40)]
    y_train = [0] * 30 + [1] * 10          # редкий позитив → balanced инфлирует p_raw
    clf, vec = fit_logistic(rows_train, y_train)
    p_tr = predict_logistic(clf, vec, rows_train)
    calibrator = IsotonicRegression(out_of_bounds="clip").fit(p_tr, y_train)

    blobs = {
        "logistic": serialize_pickle({"clf": clf, "vec": vec}),
        "calibrator": serialize_pickle(calibrator),
        "tweedie": serialize_pickle({"model": None, "vec": _NoopTweedieVec()}),
        "manifest": serialize_pickle({"names": ["x"], "cat_names": [], "point": "at_creation"}),
    }

    monkeypatch.setattr(db, "load_latest_passing_artifacts", lambda point: ("V1", blobs))
    captured = {}

    def _fake_upsert(score_rows):
        captured["rows"] = score_rows
        return len(score_rows)

    monkeypatch.setattr(db, "upsert_lead_scores", _fake_upsert)

    # Скорим ту же популяцию x=0..39 (та же область, где balanced-логистика инфлирует p_raw
    # у не-крайних точек, например x=29 — на границе классов).
    pop_rows = [{"lead_id": f"L{i}", "features": {}} for i in range(40)]
    # build_stage_matrix требует поля из REGISTRY — монки-патчим его в ml_score,
    # чтобы изолировать проверку калибровки от REGISTRY-специфики полей.
    import sync.ml_score as ml_score_mod

    def _fake_build_stage_matrix(feature_dicts, point):
        xs = [{"x": float(i)} for i in range(len(feature_dicts))]
        return xs, ["x"], []

    monkeypatch.setattr(ml_score_mod, "build_stage_matrix", _fake_build_stage_matrix)

    res = _score_point("at_creation", pop_rows)

    assert res is not None
    p_raw_expected = predict_logistic(clf, vec, [{"x": float(i)} for i in range(40)])
    p_cal_expected = calibrator.predict(p_raw_expected)
    got_p_pay = np.array([r["p_pay"] for r in captured["rows"]])

    assert np.allclose(got_p_pay, p_cal_expected, atol=1e-9)
    # у точки на границе классов (x=29, последний из «paid=0» блока) калибровка заметно
    # отличается от сырой p — здесь balanced-логистика ощутимо инфлирует
    assert abs(got_p_pay[29] - p_raw_expected[29]) > 1e-3
    assert np.allclose(np.asarray(res["p_final"]), p_cal_expected, atol=1e-9)


def test_score_point_falls_back_to_raw_when_calibrator_missing(monkeypatch):
    """Обратная совместимость: старый артефакт без ключа 'calibrator' не должен падать —
    p_pay = сырые p логистики."""
    from sync import db
    from sync.ml_score import _score_point
    import sync.ml_score as ml_score_mod

    rows_train = [{"x": float(i)} for i in range(20)]
    y_train = [0] * 10 + [1] * 10
    clf, vec = fit_logistic(rows_train, y_train)

    blobs = {
        "logistic": serialize_pickle({"clf": clf, "vec": vec}),
        "tweedie": serialize_pickle({"model": None, "vec": _NoopTweedieVec()}),
        "manifest": serialize_pickle({"names": ["x"], "cat_names": [], "point": "at_creation"}),
    }

    monkeypatch.setattr(db, "load_latest_passing_artifacts", lambda point: ("V1", blobs))
    captured = {}
    monkeypatch.setattr(db, "upsert_lead_scores",
                         lambda score_rows: captured.setdefault("rows", score_rows) and len(score_rows))

    def _fake_build_stage_matrix(feature_dicts, point):
        return [{"x": 5.0}], ["x"], []

    monkeypatch.setattr(ml_score_mod, "build_stage_matrix", _fake_build_stage_matrix)

    res = _score_point("at_creation", [{"lead_id": "L1", "features": {}}])

    assert res is not None
    p_raw_expected = predict_logistic(clf, vec, [{"x": 5.0}])
    assert abs(captured["rows"][0]["p_pay"] - float(p_raw_expected[0])) < 1e-9
