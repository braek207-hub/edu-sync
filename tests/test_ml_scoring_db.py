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
