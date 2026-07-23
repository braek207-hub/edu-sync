import os
import pytest
from sync import db

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL"
)

def test_load_vuz_lead_frame_shape():
    rows = db.load_vuz_lead_frame()
    assert len(rows) > 1000
    r = rows[0]
    for key in ("lead_id", "client_id", "created_date", "is_paid", "dispatcher"):
        assert key in r

def test_upsert_and_maturation_roundtrip():
    db.ensure_ml_feature_tables()
    n = db.upsert_lead_features([{
        "lead_id": "TEST_ML_1", "client_id": "c1", "land": "vuz",
        "created_date": "2026-01-01", "features": {"f__beh_visits": 3},
        "label_paid": None, "label_connected": True, "label_deal": False,
        "is_matured": False, "amount": None, "days_to_pay": None,
    }])
    assert n == 1
    m = db.replace_ml_maturation("vuz", [(0, 0.0), (1, 0.5), (2, 1.0)])
    assert m == 3
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM edu_lead_features WHERE lead_id='TEST_ML_1'")
        conn.commit()
