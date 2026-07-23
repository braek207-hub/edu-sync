from datetime import date
from sync.ml.features import days_to_deadline, clean_cat, derive_labels, build_feature_rows

DEADLINES = [date(2025, 8, 20), date(2026, 8, 20)]

def test_days_to_deadline_upcoming():
    assert days_to_deadline(date(2026, 8, 10), DEADLINES) == 10

def test_days_to_deadline_picks_nearest_future():
    assert days_to_deadline(date(2025, 8, 21), DEADLINES) == 364  # до 2026-08-20

def test_days_to_deadline_all_past_returns_negative():
    assert days_to_deadline(date(2026, 8, 25), DEADLINES) == -5   # 5 дней после последнего


def test_clean_cat_nulls_placeholders():
    assert clean_cat("(not set)") is None
    assert clean_cat("  ") is None
    assert clean_cat("0") is None
    assert clean_cat("Москва") == "Москва"

def test_labels_matured_paid():
    lead = {
        "is_paid": True, "is_connected": True, "is_deal": True, "amount": 120000.0,
        "created_date": date(2025, 1, 1), "payment_date": date(2025, 1, 5),
    }
    out = derive_labels(lead, today=date(2026, 7, 23), maturity_days=90)
    assert out["is_matured"] is True
    assert out["label_paid"] is True
    assert out["days_to_pay"] == 4

def test_labels_young_cohort_is_censored():
    lead = {
        "is_paid": False, "is_connected": True, "is_deal": False, "amount": None,
        "created_date": date(2026, 7, 20), "payment_date": None,
    }
    out = derive_labels(lead, today=date(2026, 7, 23), maturity_days=90)
    assert out["is_matured"] is False
    assert out["label_paid"] is None            # цензурировано, НЕ False
    assert out["label_connected"] is True       # промежуточные метки известны сразу

def test_build_rows_missing_behavior_flagged():
    leads = [{
        "lead_id": "L1", "client_id": "", "land": "vuz",
        "created_date": date(2026, 6, 1), "created_hour": 13,
        "is_paid": False, "is_connected": False, "is_deal": False,
        "payment_date": None, "amount": None,
        "audience": "parent", "b24_grad_year": "2025", "b24_edu_level": "school",
        "city_ip_segment": "rf", "direction": "it", "product_group": "vo",
        "utm_source": "yandex", "connection_date": None,
        "dispatcher": None, "responsible": None,
    }]
    rows = build_feature_rows(leads, behavior_by_client={}, deadlines=[date(2026, 8, 20)], today=date(2026, 7, 23))
    assert len(rows) == 1
    feats = rows[0]["features"]
    assert feats["f__missing_behavior"] == 1
    assert feats["f__beh_visits"] == 0
    assert "f__time_to_connection_days" in feats  # None допустимо (нет дозвона)
    assert rows[0]["label_paid"] is None          # 2026-06-01 младше 90 дней? нет — проверь maturity
