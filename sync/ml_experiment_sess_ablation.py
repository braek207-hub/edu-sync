"""Разовый ablation (НЕ прод): разбавляют ли новые sess_* фичи логистику?

Фаза B подняла покрытие поведением 26%→72%, но AP не вырос (post_connection просел
0.063→0.054). Гипотеза: высококардинальные категориальные sess_* (UTM/Директ/бакеты)
разбавляют линейную модель. Тест: обучить логистику НА ТОМ ЖЕ holdout с полным набором
фич vs БЕЗ sess_* (=пре-Фаза-B набор + session-реконструкция beh_*), сравнить AP.

Если AP_nosess > AP_full → sess_* разбавляют, отсекаем. Если ≈ → потолок сигнала.
"""

from datetime import date

from sklearn.metrics import average_precision_score

from sync import db
from sync.ml.baseline import fit_logistic, pilot_score, predict_logistic
from sync.ml.cascade import build_stage_matrix
from sync.ml_train import POINTS, point_subset, time_split


def _drop_sess(rows):
    """Убрать все f__sess_* (в матрице имена уже без f__ префикса → 'sess_*')."""
    return [{k: v for k, v in r.items() if not k.startswith("sess_")} for r in rows]


def run():
    rows = db.load_feature_matrix()
    today = date.today()
    print(f"=== sess_* ablation (v{today:%Y%m%d}) строк={len(rows)} ===")
    for point in POINTS:
        pop = point_subset(rows, point)
        train, test = time_split(pop, today=today)
        mtr = [r for r in train if r["is_matured"]]
        mte = [r for r in test if r["is_matured"]]
        if not mtr or not mte:
            print(f"[{point}] пусто — пропуск")
            continue
        y = [1 if r["label_paid"] else 0 for r in mtr]
        y_te = [1 if r["label_paid"] else 0 for r in mte]
        if sum(y) == 0 or sum(y_te) == 0:
            print(f"[{point}] нет позитивов — пропуск")
            continue

        Xtr, _, _ = build_stage_matrix([r["features"] for r in mtr], point)
        Xte, _, _ = build_stage_matrix([r["features"] for r in mte], point)

        clf_f, vec_f = fit_logistic(Xtr, y)
        ap_full = average_precision_score(y_te, predict_logistic(clf_f, vec_f, Xte))

        clf_n, vec_n = fit_logistic(_drop_sess(Xtr), y)
        ap_nosess = average_precision_score(y_te, predict_logistic(clf_n, vec_n, _drop_sess(Xte)))

        ap_pilot = average_precision_score(y_te, pilot_score([r["features"] for r in mte]))
        n_sess = sum(1 for k in Xtr[0] if k.startswith("sess_")) if Xtr else 0
        winner = "NO_SESS" if ap_nosess > ap_full else "FULL"
        print(
            f"[{point}] pos_holdout={sum(y_te)} sess_фич={n_sess} | "
            f"AP_pilot={ap_pilot:.4f} AP_full={ap_full:.4f} AP_nosess={ap_nosess:.4f} | "
            f"Δ(nosess−full)={ap_nosess - ap_full:+.4f} → {winner}"
        )


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    run()
