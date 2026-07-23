from datetime import date
from sync import ml_features_build as b
from sync.ml.registry import select_features, feature_key
from sync.ml.features import build_feature_rows

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


def test_assemble_excludes_young_payers():
    today = date(2026, 7, 23)
    young_created = date(2026, 7, 20)
    leads = [
        {
            "lead_id": "L1", "client_id": "c1", "land": "vuz",
            "created_date": date(2025, 1, 1), "created_hour": 10,
            "connection_date": date(2025, 1, 2), "payment_date": date(2025, 1, 5),
            "is_paid": True, "is_connected": True, "is_deal": True, "amount": 100000.0,
            "audience": "parent", "b24_grad_year": "2025", "b24_edu_level": "school",
            "city_ip_segment": "rf", "direction": "it", "product_group": "vo",
            "utm_source": "yandex", "dispatcher": "Иванова", "responsible": "Петров",
        },
        {
            "lead_id": "L2", "client_id": "c2", "land": "vuz",
            "created_date": young_created, "created_hour": 11,
            "connection_date": young_created, "payment_date": date(2026, 7, 21),
            "is_paid": True, "is_connected": True, "is_deal": True, "amount": 50000.0,
            "audience": "parent", "b24_grad_year": "2025", "b24_edu_level": "school",
            "city_ip_segment": "rf", "direction": "it", "product_group": "vo",
            "utm_source": "yandex", "dispatcher": "Иванова", "responsible": "Петров",
        },
    ]
    behavior = {}
    rows, maturation = b.assemble(leads, behavior, deadlines=[date(2025, 8, 20)], today=today)
    young_row = next(r for r in rows if r["lead_id"] == "L2")
    assert young_row["is_matured"] is False
    assert young_row["days_to_pay"] == 1
    # молодой плательщик (days_to_pay=1) не должен разбавлять кривую —
    # только созревший (days_to_pay=4) формирует maturation
    assert maturation[4][1] == 1.0
    assert maturation[1][1] == 0.0


def test_registry_feature_keys_present_in_build_feature_rows():
    """Кросс-модульный контракт: реестр (логические имена) и сборка фич
    (JSONB-ключи с префиксом f__) не должны рассинхронизироваться."""
    lead = {
        "lead_id": "L1", "client_id": "c1", "land": "vuz",
        "created_date": date(2025, 1, 1), "created_hour": 10,
    }
    rows = build_feature_rows([lead], behavior_by_client={}, deadlines=[date(2025, 8, 20)],
                              today=date(2026, 7, 23))
    features = rows[0]["features"]
    for name in select_features("post_connection"):
        assert feature_key(name) in features, f"missing {feature_key(name)} in build_feature_rows output"
