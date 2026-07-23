import os
import pytest
from sync import db

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL")

def test_ensure_ml_scoring_tables_idempotent():
    db.ensure_ml_scoring_tables()
    db.ensure_ml_scoring_tables()
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public' "
            "AND table_name IN ('edu_ml_artifacts','edu_ml_runs','edu_lead_scores','edu_revenue_forecast')"
        )
        got = {r[0] for r in cur.fetchall()}
    assert got == {"edu_ml_artifacts", "edu_ml_runs", "edu_lead_scores", "edu_revenue_forecast"}


def test_artifact_and_run_roundtrip():
    db.ensure_ml_scoring_tables()
    db.save_artifact("TEST_V", "manifest", b"\x00\x01\x02")
    db.insert_ml_run({
        "version": "TEST_V", "n_train": 100, "n_pos_pay": 5,
        "prauc_pay": 0.1, "brier_pay": 0.2, "lift_final": 3.0,
        "lift_baseline": 2.0, "lift_pilot": 1.5, "gate_passed": True,
        "stage_metrics": {"connect": {"prauc": 0.5}},
    })
    got = db.load_latest_passing_artifacts()
    assert got is not None and got[0] == "TEST_V"
    assert got[1]["manifest"] == b"\x00\x01\x02"
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM edu_ml_artifacts WHERE version='TEST_V'")
        cur.execute("DELETE FROM edu_ml_runs WHERE version='TEST_V'")
        conn.commit()
