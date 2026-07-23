import os
import pytest
from sync import db

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"
)

def test_ensure_ml_feature_tables_idempotent():
    db.ensure_ml_feature_tables()
    db.ensure_ml_feature_tables()  # второй вызов не падает
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name IN "
            "('edu_lead_features','edu_ml_maturation')"
        )
        got = {r[0] for r in cur.fetchall()}
    assert got == {"edu_lead_features", "edu_ml_maturation"}
