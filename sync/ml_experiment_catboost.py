"""Разовый A/B-эксперимент (НЕ прод-путь): single-stage CatBoost против прод-логистики
на ОДНИХ данных, сплите и признаках. Отвечает на вопрос: бьёт ли градиентный бустинг
линейную модель на нашей выборке ПОСЛЕ бэкфилла поведения (2024–2026).

Раньше гейт проигрывал 3-СТУПЕНЧАТЫЙ каскад CatBoost — это смешивало алгоритм и
архитектуру. Здесь честно: одинаковый DictVectorized-вход для обеих моделей, отличие
только в алгоритме. Ничего не пишет в БД/артефакты — только печатает AP.

Запуск: CI (workflow experiment-catboost.yml), т.к. catboost не установлен локально.
"""

from datetime import date

import numpy as np
from sklearn.feature_extraction import DictVectorizer
from sklearn.metrics import average_precision_score

from sync import db
from sync.ml.baseline import fit_logistic, pilot_score, predict_logistic
from sync.ml.cascade import build_stage_matrix
from sync.ml_train import POINTS, point_subset, time_split


def _matured(rows):
    return [r for r in rows if r.get("is_matured")]


def _fit_catboost(Xtr_dicts, y, Xte_dicts):
    """CatBoost на той же DictVectorized-матрице, что и логистика (идентичный вход).
    Гиперпараметры под режим МАЛО ПОЗИТИВОВ (~300): мелкие деревья + сильный L2 против
    переобучения (CatBoost docs: для малых датасетов depth 4–6, повышенный l2_leaf_reg;
    auto_class_weights=Balanced — аналог class_weight='balanced' логистики для дисбаланса)."""
    from catboost import CatBoostClassifier

    vec = DictVectorizer(sparse=False)
    Xtr = vec.fit_transform(Xtr_dicts)
    Xte = vec.transform(Xte_dicts)
    clf = CatBoostClassifier(
        iterations=400,
        depth=4,
        learning_rate=0.03,
        l2_leaf_reg=6.0,
        loss_function="Logloss",
        auto_class_weights="Balanced",
        random_seed=42,
        verbose=False,
    )
    clf.fit(Xtr, np.asarray(y, dtype=int))
    return clf.predict_proba(Xte)[:, 1]


def run_experiment():
    rows = db.load_feature_matrix()
    today = date.today()
    print(f"=== A/B CatBoost vs Logistic (v{today:%Y%m%d}) — всего строк {len(rows)} ===")

    for point in POINTS:
        pop = point_subset(rows, point)
        train, test = time_split(pop, today=today)
        mtr, mte = _matured(train), _matured(test)
        if not mtr or not mte:
            print(f"[{point}] пустой train/test — пропуск")
            continue
        y = [1 if r["label_paid"] else 0 for r in mtr]
        y_te = [1 if r["label_paid"] else 0 for r in mte]
        if sum(y) == 0 or sum(y_te) == 0:
            print(f"[{point}] нет позитивов — пропуск")
            continue

        Xtr, _, _ = build_stage_matrix([r["features"] for r in mtr], point)
        Xte, _, _ = build_stage_matrix([r["features"] for r in mte], point)

        clf, vec = fit_logistic(Xtr, y)
        ap_log = average_precision_score(y_te, predict_logistic(clf, vec, Xte))
        ap_cb = average_precision_score(y_te, _fit_catboost(Xtr, y, Xte))
        ap_pilot = average_precision_score(y_te, pilot_score([r["features"] for r in mte]))

        winner = "CatBoost" if ap_cb > ap_log else "Logistic"
        print(
            f"[{point}] n_train={len(mtr)} pos_train={sum(y)} pos_holdout={sum(y_te)} | "
            f"AP_pilot={ap_pilot:.4f} AP_logistic={ap_log:.4f} AP_catboost={ap_cb:.4f} | "
            f"gate_logistic={ap_log > ap_pilot} gate_catboost={ap_cb > ap_pilot} | "
            f"WINNER={winner} (Δ={ap_cb - ap_log:+.4f})"
        )


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    run_experiment()
