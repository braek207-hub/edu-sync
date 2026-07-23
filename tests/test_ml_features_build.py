from datetime import date
from sync import ml_features_build as b

def test_assemble_pure(monkeypatch):
    leads = [{
        "lead_id": "L1", "client_id": "c1", "land": "vuz",
        "created_date": date(2025, 1, 1), "created_hour": 10,
        "connection_date": date(2025, 1, 2), "payment_date": date(2025, 1, 5),
        "is_paid": True, "is_connected": True, "is_deal": True, "amount": 120000.0,
        "audience": "parent", "b24_grad_year": "2025", "b24_edu_level": "school",
        "city_ip_segment": "rf", "direction": "it", "product_group": "vo",
        "utm_source": "yandex", "dispatcher": "Иванова", "responsible": "Петров",
    }]
    behavior = {"c1": {"visits": 5, "visit_days": 2, "avg_duration_sec": 200.0,
                       "bounce_rate": 10.0, "page_depth": 3.0, "device": "desktop", "source": "ad"}}
    rows, maturation = b.assemble(leads, behavior, deadlines=[date(2025, 8, 20)], today=date(2026, 7, 23))
    assert rows[0]["label_paid"] is True
    assert rows[0]["features"]["f__missing_behavior"] == 0
    assert rows[0]["features"]["f__time_to_connection_days"] == 1
    assert maturation[0] == (0, 0.0)
    assert maturation[4][1] == 1.0   # единственная оплата в день 4 → к дню 4 доля 1.0
