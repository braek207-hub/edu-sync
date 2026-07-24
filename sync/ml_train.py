"""Оркестратор обучения EDU (land=vuz). Гоняется в CI (train-edu-ml.yml):
load_feature_matrix → time-split → single-stage логистика P(pay) → метрики + гейт →
артефакты + edu_ml_runs. Обучает ДВЕ точки скоринга: at_creation (все лиды) и
post_connection (только дозвонившиеся). Ф2.1: каскад заменён на прямую логистику
(бьёт каскад на holdout на обеих точках); пилот-эвристика остаётся планкой гейта.
Прод-путь не тянет catboost — модель на sklearn, тестируется локально."""

from datetime import date, timedelta
from typing import Any, Optional

from sync import db
from sync.ml.artifacts import serialize_pickle
from sync.ml.baseline import fit_logistic, pilot_score, predict_logistic
from sync.ml.cascade import build_stage_matrix
from sync.ml.metrics import brier, lift_at_decile

HOLDOUT_MONTHS = 3
MATURITY_DAYS = 90
POINTS = ["at_creation", "post_connection"]
SUFFIX = {"at_creation": "atc", "post_connection": "pc"}


def point_subset(rows, point):
    """Субпопуляция под точку скоринга: at_creation = все; post_connection = дозвонившиеся."""
    if point == "post_connection":
        return [r for r in rows if r.get("label_connected")]
    return list(rows)


def decide_gate(ap_model: float, ap_pilot: float) -> bool:
    """Гейт Ф2.1: модель публикуется, только если её AP строго бьёт бесплатную
    пилот-эвристику на созревшем holdout."""
    return ap_model > ap_pilot


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


def _train_one_point(rows, point: str, today: date) -> dict[str, Any]:
    """Прод-путь Ф2.1: single-stage логистика P(pay) на созревших train, оценка AP
    на созревшем holdout, гейт = AP модели > AP пилота. + Tweedie-прогноз чека."""
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.linear_model import TweedieRegressor
    from sklearn.metrics import average_precision_score

    pop = point_subset(rows, point)
    train, test = time_split(pop, today=today)
    version = today.strftime("%Y%m%d")

    def _degenerate(reason):
        return {"run": {"version": version, "scoring_point": point,
                        "n_train": len(train), "n_pos_pay": 0, "prauc_pay": None,
                        "brier_pay": None, "lift_final": 0.0, "lift_baseline": None,
                        "lift_pilot": 0.0, "gate_passed": False,
                        "stage_metrics": {"skipped": reason, "model": "logistic_single_stage"}},
                "artifacts": {}}

    if not train or not test:
        return _degenerate("empty train/test after split")

    # ── обучение на СОЗРЕВШИХ train (метка pay наблюдаема) ──
    mtr = [r for r in train if r["is_matured"]]
    y = [1 if r["label_paid"] else 0 for r in mtr]
    if len(mtr) == 0 or sum(y) == 0 or sum(y) == len(y):
        return _degenerate("empty/single-class matured train")
    Xtr, names, cat_names = build_stage_matrix([r["features"] for r in mtr], point)
    clf, vec = fit_logistic(Xtr, y)

    # ── оценка на СОЗРЕВШЕМ holdout ──
    mte = [r for r in test if r["is_matured"]]
    if not mte:
        return _degenerate("empty matured holdout")
    Xte, _, _ = build_stage_matrix([r["features"] for r in mte], point)
    p = predict_logistic(clf, vec, Xte)
    y_te = [1 if r["label_paid"] else 0 for r in mte]
    has_pos = sum(y_te) > 0

    ap_model = float(average_precision_score(y_te, p)) if has_pos else 0.0
    pilot = pilot_score([r["features"] for r in mte])
    ap_pilot = float(average_precision_score(y_te, pilot)) if has_pos else 0.0
    gate = decide_gate(ap_model, ap_pilot)

    # ── Tweedie для чека (на созревших train paid) ──
    paid_tr = [(Xtr[i], float(mtr[i]["amount"]))
               for i in range(len(mtr)) if mtr[i]["label_paid"] and mtr[i]["amount"]]
    tw_vec = DictVectorizer(sparse=False)
    if paid_tr:
        Xtw = tw_vec.fit_transform([r for r, _ in paid_tr])
        tw = TweedieRegressor(power=1.5, link="log", max_iter=1000)
        tw.fit(Xtw, [a for _, a in paid_tr])
    else:
        tw = None

    artifacts = {
        "logistic": serialize_pickle({"clf": clf, "vec": vec}),
        "tweedie": serialize_pickle({"model": tw, "vec": tw_vec}),
        "manifest": serialize_pickle({"names": names, "cat_names": cat_names, "point": point}),
    }
    run = {
        "version": version, "scoring_point": point, "n_train": len(mtr),
        "n_pos_pay": int(sum(y_te)), "prauc_pay": ap_model, "brier_pay": brier(y_te, p),
        "lift_final": lift_at_decile(y_te, p), "lift_baseline": None,
        "lift_pilot": lift_at_decile(y_te, pilot), "gate_passed": gate,
        "stage_metrics": {"ap_model": ap_model, "ap_pilot": ap_pilot,
                          "base_rate": sum(y_te) / len(y_te),
                          "model": "logistic_single_stage"},
    }
    return {"run": run, "artifacts": artifacts}


def train_and_eval(rows, today: Optional[date] = None) -> list[dict[str, Any]]:
    """Обучает обе точки скоринга; возвращает список результатов per point."""
    today = today or date.today()
    return [_train_one_point(rows, point, today) for point in POINTS]


def run_training() -> list[dict[str, Any]]:
    db.ensure_ml_scoring_tables()
    rows = db.load_feature_matrix()
    results = train_and_eval(rows)
    runs = []
    for res in results:
        r = res["run"]
        sfx = SUFFIX[r["scoring_point"]]
        for kind, blob in res["artifacts"].items():
            db.save_artifact(r["version"], f"{kind}_{sfx}", blob)
        db.insert_ml_run(r)
        sm = r["stage_metrics"]
        print(f"train v{r['version']} [{r['scoring_point']}]: "
              f"ap_model={sm.get('ap_model')} ap_pilot={sm.get('ap_pilot')} "
              f"lift_final={r['lift_final']:.2f} pilot={r['lift_pilot']:.2f} "
              f"gate={r['gate_passed']}")
        runs.append(r)
    return runs


if __name__ == "__main__":
    run_training()
