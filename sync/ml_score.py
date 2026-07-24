"""Оркестратор скоринга+прогноза EDU (land=vuz). Гоняется в CI (score-edu-ml.yml):
грузит последнюю прошедшую гейт модель ПО КАЖДОЙ точке → скорит субпопуляцию →
edu_lead_scores(scoring_point) → прогноз (только at_creation) → edu_revenue_forecast.
Публикует точку ТОЛЬКО если есть её версия с gate_passed=true."""

from datetime import date, timedelta
from typing import Any, Optional

import numpy as np

from sync import db
from sync.ml.artifacts import deserialize_pickle
from sync.ml.baseline import logistic_top_factors, predict_logistic
from sync.ml.cascade import build_stage_matrix
from sync.ml.forecast import aggregate_forecast, expected_revenue
from sync.ml_train import POINTS, point_subset

MATURITY_DAYS = 90


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


def _score_point(point: str, rows: list) -> Optional[dict[str, Any]]:
    """Скорит субпопуляцию точки, если есть прошедшая гейт модель. Возвращает
    {version, pop, X, p_final, tw, n} для последующего прогноза, либо None (нет модели)."""
    loaded = db.load_latest_passing_artifacts(point)
    if loaded is None:
        print(f"нет модели {point} — пропуск")
        return None
    version, blobs = loaded
    lg = deserialize_pickle(blobs["logistic"])
    clf, vec = lg["clf"], lg["vec"]
    tw = deserialize_pickle(blobs["tweedie"])

    pop = point_subset(rows, point)
    X, _, _ = build_stage_matrix([r["features"] for r in pop], point)
    p = predict_logistic(clf, vec, X)
    deciles = to_deciles(p)

    score_rows = []
    for i, r in enumerate(pop):
        # топ-3 фактора: coef[j]*x[j] линейного логита (точная замена SHAP)
        top = logistic_top_factors(clf, vec, X[i])
        score_rows.append({
            "lead_id": r["lead_id"], "scoring_point": point,
            "p_connect": None, "p_deal": None, "p_pay": float(p[i]),
            "decile": deciles[i], "top_shap": top, "model_version": version,
        })
    n = db.upsert_lead_scores(score_rows)
    return {"version": version, "pop": pop, "X": X, "p_final": p, "tw": tw, "n": n}


def run_scoring(today: Optional[date] = None) -> dict[str, Any]:
    today = today or date.today()
    db.ensure_ml_scoring_tables()
    rows = db.load_feature_matrix()

    total_scored = 0
    atc = None
    for point in POINTS:
        res = _score_point(point, rows)
        if res is None:
            continue
        total_scored += res["n"]
        if point == "at_creation":
            atc = res

    # ── прогноз выручки (только at_creation — вся популяция лидов) ──
    k = 0
    if atc is None:
        print("нет модели at_creation — прогноз пропущен")
    else:
        version, pop, X, p_final = atc["version"], atc["pop"], atc["X"], atc["p_final"]
        remaining = _maturation_remaining(today)
        tw_model, tw_vec = atc["tw"]["model"], atc["tw"]["vec"]
        fc_items = []
        for i, r in enumerate(pop):
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

    print(f"score: leads={total_scored} forecast_segments={k}")
    return {"scored": total_scored, "forecast_segments": k}


if __name__ == "__main__":
    run_scoring()
