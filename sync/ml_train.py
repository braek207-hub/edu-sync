"""Оркестратор обучения каскада EDU (land=vuz). Гоняется в CI (train-edu-ml.yml):
load_feature_matrix → time-split → 3 CatBoost + isotonic → метрики + гейт → артефакты + edu_ml_runs.
Локально не запускается (БД недоступна)."""

from datetime import date, timedelta
from typing import Any, Optional

import numpy as np

from sync import db
from sync.ml.artifacts import serialize_catboost, serialize_pickle
from sync.ml.baseline import fit_logistic_baseline, pilot_score
from sync.ml.cascade import build_stage_matrix, compose_cascade
from sync.ml.metrics import brier, lift_at_decile

HOLDOUT_MONTHS = 3
MATURITY_DAYS = 90
POINT = "at_creation"


def decide_gate(lift_final: float, lift_baseline: float, lift_pilot: float) -> bool:
    return lift_final > max(lift_baseline, lift_pilot)


def time_split(rows, holdout_months=HOLDOUT_MONTHS, maturity_days=MATURITY_DAYS, today=None):
    """Только созревшие когорты (метка pay наблюдаема). test = held-out окно
    [today-(maturity+holdout), today-maturity): все is_matured, не в train.
    train = старше него. Молодые (age<maturity) исключены — оценивать P(pay) нечем.
    Анти-утечка по человеку (цель GroupKFold, I1): из test убираем лиды с client_id из train."""
    today = today or date.today()
    end = today - timedelta(days=maturity_days)
    start = today - timedelta(days=maturity_days + 30 * holdout_months)
    train = [r for r in rows if r["created_date"] < start]
    test = [r for r in rows if start <= r["created_date"] < end]
    train_clients = {r.get("client_id") for r in train if r.get("client_id")}
    test = [r for r in test if not (r.get("client_id") and r["client_id"] in train_clients)]
    return train, test


def _fit_stage(rows, cat_names, y, spw):
    from catboost import CatBoostClassifier, Pool
    names = list(rows[0].keys()) if rows else []
    cat_idx = [names.index(c) for c in cat_names]
    X = [[r[n] for n in names] for r in rows]
    pool = Pool(X, label=list(y), cat_features=cat_idx)
    m = CatBoostClassifier(iterations=300, depth=6, learning_rate=0.05,
                           loss_function="Logloss", scale_pos_weight=spw,
                           verbose=False, random_seed=42)
    m.fit(pool)
    return m, names, cat_idx


def _proba(model, rows, names, cat_idx):
    from catboost import Pool
    X = [[r[n] for n in names] for r in rows]
    return model.predict_proba(Pool(X, cat_features=cat_idx))[:, 1]


def train_and_eval(rows, today: Optional[date] = None) -> dict[str, Any]:
    from sklearn.isotonic import IsotonicRegression
    today = today or date.today()
    train, test = time_split(rows, today=today)

    def _degenerate(reason):
        return {"run": {"version": today.strftime("%Y%m%d"), "n_train": len(train),
                        "n_pos_pay": 0, "prauc_pay": None, "brier_pay": None,
                        "lift_final": 0.0, "lift_baseline": 0.0, "lift_pilot": 0.0,
                        "gate_passed": False, "stage_metrics": {"skipped": reason}},
                "artifacts": {}}

    if not train or not test:
        return _degenerate("empty train/test after split")

    def matrix(subset):
        feats = [r["features"] for r in subset]
        return build_stage_matrix(feats, POINT)

    # ── стадии ──
    tr_rows, names, cat_names = matrix(train)
    te_rows, _, _ = matrix(test)

    # connect: все
    y_c = [1 if r["label_connected"] else 0 for r in train]

    # deal|connect
    idx_conn = [i for i, r in enumerate(train) if r["label_connected"]]
    y_d = [1 if train[i]["label_deal"] else 0 for i in idx_conn]

    # pay|deal (только созревшие)
    idx_deal = [i for i, r in enumerate(train) if r["label_deal"] and r["is_matured"]]
    y_p = [1 if train[i]["label_paid"] else 0 for i in idx_deal]

    if (sum(y_c) == 0 or len(idx_conn) == 0 or sum(y_d) == 0
            or len(idx_deal) == 0 or sum(y_p) == 0):
        return _degenerate("single-class or empty stage subpopulation")

    spw_c = max(1.0, (len(y_c) - sum(y_c)) / max(1, sum(y_c)))
    m_c, nm, ci = _fit_stage(tr_rows, cat_names, y_c, spw_c)
    cal_c = IsotonicRegression(out_of_bounds="clip").fit(
        _proba(m_c, tr_rows, nm, ci), y_c)

    spw_d = max(1.0, (len(y_d) - sum(y_d)) / max(1, sum(y_d)))
    m_d, _, _ = _fit_stage([tr_rows[i] for i in idx_conn], cat_names, y_d, spw_d)
    cal_d = IsotonicRegression(out_of_bounds="clip").fit(
        _proba(m_d, [tr_rows[i] for i in idx_conn], nm, ci), y_d)

    spw_p = max(1.0, (len(y_p) - sum(y_p)) / max(1, sum(y_p)))
    m_p, _, _ = _fit_stage([tr_rows[i] for i in idx_deal], cat_names, y_p, spw_p)
    cal_p = IsotonicRegression(out_of_bounds="clip").fit(
        _proba(m_p, [tr_rows[i] for i in idx_deal], nm, ci), y_p)

    # ── оценка на holdout (итоговый P(pay), метка = matured & paid) ──
    te_mat = [r for r in test if r["is_matured"]]
    if not te_mat:
        return _degenerate("empty matured holdout")
    te_mat_rows, _, _ = matrix(te_mat)
    pc = cal_c.predict(_proba(m_c, te_mat_rows, nm, ci))
    pd = cal_d.predict(_proba(m_d, te_mat_rows, nm, ci))
    pp = cal_p.predict(_proba(m_p, te_mat_rows, nm, ci))
    p_final = compose_cascade(pc, pd, pp)
    y_final = [1 if r["label_paid"] else 0 for r in te_mat]

    from sklearn.metrics import average_precision_score
    lift_final = lift_at_decile(y_final, p_final)
    br = brier(y_final, p_final)

    # бейзлайны
    pilot = pilot_score([r["features"] for r in te_mat])
    lift_pilot = lift_at_decile(y_final, pilot)
    base_pred = fit_logistic_baseline(tr_rows, cat_names,
                                      [1 if r["label_paid"] else 0 for r in train])
    base_scores = base_pred(te_mat_rows)
    lift_base = lift_at_decile(y_final, base_scores)

    # PR-AUC (average precision) — не сатурирует на топ-дециле как lift; ОСНОВНАЯ метрика гейта
    has_pos = sum(y_final) > 0
    ap_final = float(average_precision_score(y_final, p_final)) if has_pos else 0.0
    ap_base = float(average_precision_score(y_final, base_scores)) if has_pos else 0.0
    ap_pilot = float(average_precision_score(y_final, pilot)) if has_pos else 0.0

    gate = decide_gate(ap_final, ap_base, ap_pilot)
    version = today.strftime("%Y%m%d")

    # ── Tweedie для чека ──
    from sklearn.linear_model import TweedieRegressor
    from sklearn.feature_extraction import DictVectorizer
    paid_tr = [(tr_rows[i], float(train[i]["amount"]))
               for i in range(len(train)) if train[i]["label_paid"] and train[i]["amount"]]
    tw_vec = DictVectorizer(sparse=False)
    if paid_tr:
        Xtw = tw_vec.fit_transform([r for r, _ in paid_tr])
        tw = TweedieRegressor(power=1.5, link="log", max_iter=1000)
        tw.fit(Xtw, [a for _, a in paid_tr])
    else:
        tw = None

    artifacts = {
        "cb_connect": serialize_catboost(m_c), "cb_deal": serialize_catboost(m_d),
        "cb_pay": serialize_catboost(m_p),
        "cal_connect": serialize_pickle(cal_c), "cal_deal": serialize_pickle(cal_d),
        "cal_pay": serialize_pickle(cal_p),
        "tweedie": serialize_pickle({"model": tw, "vec": tw_vec}),
        "manifest": serialize_pickle({"names": nm, "cat_idx": ci, "point": POINT}),
    }
    run = {
        "version": version, "n_train": len(train), "n_pos_pay": sum(y_final),
        "prauc_pay": ap_final, "brier_pay": br, "lift_final": lift_final,
        "lift_baseline": lift_base, "lift_pilot": lift_pilot, "gate_passed": gate,
        "stage_metrics": {"n_connect_pos": int(sum(y_c)), "n_deal_pos": int(sum(y_d)),
                          "n_pay_pos": int(sum(y_p)),
                          "ap_final": ap_final, "ap_base": ap_base, "ap_pilot": ap_pilot,
                          "lift_final": lift_final, "lift_base": lift_base},
    }
    return {"run": run, "artifacts": artifacts}


def run_training() -> dict[str, Any]:
    db.ensure_ml_scoring_tables()
    rows = db.load_feature_matrix()
    result = train_and_eval(rows)
    for kind, blob in result["artifacts"].items():
        db.save_artifact(result["run"]["version"], kind, blob)
    db.insert_ml_run(result["run"])
    r = result["run"]
    print(f"train v{r['version']}: lift_final={r['lift_final']:.2f} "
          f"base={r['lift_baseline']:.2f} pilot={r['lift_pilot']:.2f} gate={r['gate_passed']}")
    return r


if __name__ == "__main__":
    run_training()
