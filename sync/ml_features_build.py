"""Оркестратор сборки feature store EDU (land=vuz): читает лиды + поведение,
строит фичи и кривую созревания, апсертит в edu_lead_features / edu_ml_maturation.
Запуск: python -m sync.ml_features_build (или из workflow build-edu-features.yml)."""

import os
from datetime import date
from typing import Any, Optional

from sync import db
from sync.ml.features import build_feature_rows, load_admission_deadlines
from sync.ml.maturation import maturation_table

_CAL = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "edu_admission_calendar.json")


def assemble(leads, behavior_dated, deadlines, today):
    """Чистая часть: строки фич + кривая созревания (по созревшим оплатам)."""
    rows = build_feature_rows(leads, behavior_dated, deadlines, today)
    paid_dtp = [r["days_to_pay"] for r in rows
                if r["is_matured"] and r["label_paid"] and r["days_to_pay"] is not None]
    maturation = maturation_table(paid_dtp, horizon=120)
    return rows, maturation


def build_edu_features(today: Optional[date] = None) -> dict[str, Any]:
    today = today or date.today()
    db.ensure_ml_feature_tables()
    leads = db.load_vuz_lead_frame()
    behavior = db.load_vuz_behavior_frame()
    deadlines = load_admission_deadlines(_CAL)
    rows, maturation = assemble(leads, behavior, deadlines, today)
    n = db.upsert_lead_features(rows)
    k = db.replace_ml_maturation("vuz", maturation)
    matured = sum(1 for r in rows if r["is_matured"])
    print(f"feature store vuz: leads={n} matured={matured} maturation_points={k}")
    return {"leads": n, "matured": matured, "maturation_points": k}


if __name__ == "__main__":
    build_edu_features()
