"""Экспорт весов прод-логистики at_creation → edu_ml_weights (JSONB): реалтайм-скоринг
лида на Vercel в момент заявки (JS-цель Директа quality_lead_ml, без офлайн-конверсий).

Модель — DictVectorizer + LogisticRegression БЕЗ скейлера, поэтому скор нового лида
воспроизводится линейной формулой: p_raw = sigmoid(Σ coef[колонка]·x + intercept).
Категориальная фича кодируется колонкой "имя=значение" (0/1; значение, не виданное
на train, не даёт ни одной колонки — так же, как молча дропает vec.transform),
числовая — колонкой "имя" с самим значением.

Порог качества = «дециль ≤ 2» от СЫРОГО p_raw (децили ранговые, ml_score.to_deciles:
дециль ≤ 2 ⇔ rank < 0.2·n) → порог = минимальный p_raw топ-20% популяции. Считается
по всей edu_lead_features на момент экспорта; дрейф между еженедельными экспортами мал.

payload дополнительно несёт:
- admission_calendar — дедлайны приёмной кампании (days_to_deadline в TS без хардкода);
- samples — контрольные лиды (сырые фичи + sklearn p_raw) для паритет-теста TS-скоринга.

Запуск: python -m sync.ml_export_weights (workflow export-edu-ml-weights.yml
или шаг после тренировки в train-edu-ml.yml).
"""

from typing import Any, Sequence

import numpy as np

from sync import db
from sync.ml.artifacts import deserialize_pickle
from sync.ml.baseline import predict_logistic
from sync.ml.cascade import build_stage_matrix
from sync.ml.features import load_admission_deadlines
from sync.ml_features_build import _CAL
from sync.ml_train import point_subset

POINT = "at_creation"
N_SAMPLES = 20


def extract_weights(clf, vec) -> tuple[float, dict[str, float]]:
    """{имя_колонки DictVectorizer: вес} + intercept обученной логистики."""
    names = [str(n) for n in vec.get_feature_names_out()]
    coefs = [float(c) for c in clf.coef_[0]]
    return float(clf.intercept_[0]), dict(zip(names, coefs))


def quality_threshold(p_raw: Sequence[float], max_decile: int = 2) -> float:
    """Минимальный p_raw строк с ранговым децилем ≤ max_decile (формула
    ml_score.to_deciles: дециль = min(10, int(rank·10/n)+1), rank по убыванию p)."""
    s = np.sort(np.asarray(p_raw, dtype=float))[::-1]
    n = len(s)
    if n == 0:
        raise ValueError("пустая популяция — порог не определён")
    k = sum(1 for rank in range(n) if min(10, int(rank * 10 / n) + 1) <= max_decile)
    return float(s[k - 1])


def pick_samples(pop: list, X: list, p_raw: Sequence[float], n_samples: int = N_SAMPLES) -> list[dict]:
    """Контрольные лиды для паритет-теста TS: равномерно по популяции,
    детерминированно (сортировка по lead_id зафиксирована в run_export)."""
    n = len(pop)
    if n == 0:
        return []
    step = max(1, n // n_samples)
    idx = list(range(0, n, step))[:n_samples]
    return [
        {"lead_id": pop[i]["lead_id"], "features": X[i], "p_raw": float(p_raw[i])}
        for i in idx
    ]


def build_payload(version: str, intercept: float, coefs: dict[str, float],
                  threshold: float, deadlines: list[str], samples: list[dict],
                  n_pop: int) -> dict[str, Any]:
    return {
        "point": POINT,
        "model_version": version,
        "intercept": intercept,
        "coefs": coefs,
        "quality_threshold_p_raw": threshold,
        "quality_max_decile": 2,
        "admission_calendar": deadlines,
        "samples": samples,
        "n_population": n_pop,
    }


def run_export() -> dict[str, Any]:
    loaded = db.load_latest_passing_artifacts(POINT)
    if loaded is None:
        print(f"нет прошедшей гейт модели {POINT} — экспорт пропущен")
        return {"exported": False}
    version, blobs = loaded
    man = deserialize_pickle(blobs["manifest"])
    assert man.get("point") == POINT, f"artifact point {man.get('point')!r} != {POINT!r}"
    lg = deserialize_pickle(blobs["logistic"])
    clf, vec = lg["clf"], lg["vec"]

    rows = db.load_feature_matrix()
    pop = sorted(point_subset(rows, POINT), key=lambda r: r["lead_id"])
    X, _, _ = build_stage_matrix([r["features"] for r in pop], POINT)
    p_raw = predict_logistic(clf, vec, X)

    intercept, coefs = extract_weights(clf, vec)
    threshold = quality_threshold(p_raw)
    deadlines = [d.isoformat() for d in load_admission_deadlines(_CAL)]
    samples = pick_samples(pop, X, p_raw)
    payload = build_payload(version, intercept, coefs, threshold, deadlines, samples, len(pop))

    db.ensure_ml_weights_table()
    db.upsert_ml_weights(POINT, version, payload)
    share = float(np.mean(np.asarray(p_raw) >= threshold))
    print(f"weights export: version={version} features={len(coefs)} "
          f"threshold={threshold:.6f} (топ {share:.1%} из {len(pop)})")
    return {"exported": True, "version": version, "threshold": threshold}


if __name__ == "__main__":
    run_export()
