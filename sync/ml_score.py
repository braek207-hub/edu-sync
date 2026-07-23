"""Оркестратор скоринга+прогноза EDU (land=vuz). Гоняется в CI (score-edu-ml.yml):
грузит последнюю прошедшую гейт модель → скорит лиды → edu_lead_scores → прогноз → edu_revenue_forecast.
Публикует ТОЛЬКО если есть версия с gate_passed=true."""

from datetime import date, timedelta
from typing import Any, Optional

import numpy as np

from sync import db
from sync.ml.artifacts import deserialize_catboost, deserialize_pickle
from sync.ml.cascade import build_stage_matrix, compose_cascade
from sync.ml.forecast import aggregate_forecast, expected_revenue

MATURITY_DAYS = 90
POINT = "at_creation"


def to_deciles(scores) -> list[int]:
    s = np.asarray(scores, dtype=float)
    n = len(s)
    if n == 0:
        return []
    order = np.argsort(-s)
    out = [1] * n
    for rank, idx in enumerate(order):
        out[idx] = min(10, int(rank * 10 / n) + 1)
    return out


def is_pending(row, today: Optional[date] = None, maturity_days: int = MATURITY_DAYS) -> bool:
    today = today or date.today()
    if row.get("label_paid"):
        return False
    age = (today - row["created_date"]).days
    return age < maturity_days


def _maturation_remaining(today: date) -> dict[int, float]:
    """age_days → (1 − matured_fraction): доля ещё-не-наступивших оплат."""
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT age_days, matured_fraction FROM edu_ml_maturation WHERE land='vuz'")
        return {int(a): max(0.0, 1.0 - float(f)) for a, f in cur.fetchall()}


def run_scoring(today: Optional[date] = None) -> dict[str, Any]:
    today = today or date.today()
    db.ensure_ml_scoring_tables()
    loaded = db.load_latest_passing_artifacts()
    if loaded is None:
        print("нет модели с gate_passed=true — скоринг пропущен")
        return {"scored": 0, "forecast_segments": 0}
    version, blobs = loaded
    man = deserialize_pickle(blobs["manifest"])
    nm, ci = man["names"], man["cat_idx"]
    m_c = deserialize_catboost(blobs["cb_connect"])
    m_d = deserialize_catboost(blobs["cb_deal"])
    m_p = deserialize_catboost(blobs["cb_pay"])
    cal_c = deserialize_pickle(blobs["cal_connect"])
    cal_d = deserialize_pickle(blobs["cal_deal"])
    cal_p = deserialize_pickle(blobs["cal_pay"])
    tw = deserialize_pickle(blobs["tweedie"])

    rows = db.load_feature_matrix()
    feats = [r["features"] for r in rows]
    X, _, _ = build_stage_matrix(feats, POINT)
    from catboost import Pool
    Xm = [[r[n] for n in nm] for r in X]
    pc = cal_c.predict(m_c.predict_proba(Pool(Xm, cat_features=ci))[:, 1])
    pd = cal_d.predict(m_d.predict_proba(Pool(Xm, cat_features=ci))[:, 1])
    pp = cal_p.predict(m_p.predict_proba(Pool(Xm, cat_features=ci))[:, 1])
    p_final = compose_cascade(pc, pd, pp)
    deciles = to_deciles(p_final)

    # SHAP (нативный CatBoost) для стадии pay — топ-3 фактора на лид
    shap = m_p.get_feature_importance(type="ShapValues", data=Pool(Xm, cat_features=ci))

    score_rows = []
    for i, r in enumerate(rows):
        contrib = shap[i][:-1]  # последний столбец — expected value
        top_idx = np.argsort(-np.abs(contrib))[:3]
        top = [{"feature": nm[j], "shap": float(contrib[j])} for j in top_idx]
        score_rows.append({
            "lead_id": r["lead_id"], "scoring_point": POINT,
            "p_connect": float(pc[i]), "p_deal": float(pd[i]), "p_pay": float(p_final[i]),
            "decile": deciles[i], "top_shap": top, "model_version": version,
        })
    n = db.upsert_lead_scores(score_rows)

    # ── прогноз выручки ──
    remaining = _maturation_remaining(today)
    tw_model, tw_vec = tw["model"], tw["vec"]
    fc_items = []
    for i, r in enumerate(rows):
        if not is_pending(r, today):
            continue
        age = (today - r["created_date"]).days
        rem = remaining.get(age, remaining.get(min(remaining, key=lambda k: abs(k - age)), 0.0)) if remaining else 0.0
        exp_amt = float(tw_model.predict(tw_vec.transform([X[i]]))[0]) if tw_model else 0.0
        exp_rev = expected_revenue(p_final[i], exp_amt, rem)
        fc_items.append({"segment": r["direction"] or "__na__", "exp_rev": exp_rev, "p_pay": float(p_final[i])})

    fc_rows = aggregate_forecast(fc_items)
    for fr in fc_rows:
        fr["as_of_date"] = today
        fr["model_version"] = version
    k = db.upsert_revenue_forecast(fc_rows)
    print(f"score v{version}: leads={n} forecast_segments={k}")
    return {"scored": n, "forecast_segments": k}


if __name__ == "__main__":
    run_scoring()
