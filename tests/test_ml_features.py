from datetime import date, datetime
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
    rows = build_feature_rows(leads, sessions={}, deadlines=[date(2026, 8, 20)], today=date(2026, 7, 23))
    assert len(rows) == 1
    feats = rows[0]["features"]
    assert feats["f__missing_behavior"] == 1
    assert feats["f__beh_visits"] == 0
    assert "f__time_to_connection_days" in feats  # None допустимо (нет дозвона)
    assert rows[0]["label_paid"] is None          # 2026-06-01 младше 90 дней? нет — проверь maturity


def test_timing_and_time_aware_behavior():
    """Ф2 (Task 5): behavior_dated (дневной агрегат) → sessions (per-visit, точный
    visit_ts). Дни 8/9 разложены на отдельные визиты (2+1), день 12 — после created_ts
    (13:30) → исключён, как раньше исключался день целиком."""
    lead = {
        "lead_id": "L1", "client_id": "c1", "land": "vuz",
        "created_date": date(2026, 6, 10),
        "created_ts": datetime(2026, 6, 10, 13, 30),
        "connected_ts": datetime(2026, 6, 10, 14, 0),
        "is_paid": False, "is_connected": True, "is_deal": False,
        "payment_date": None, "amount": None, "connection_date": date(2026, 6, 10),
        "audience": "parent", "b24_grad_year": "2025", "b24_edu_level": "school",
        "city_ip_segment": "rf", "direction": "it", "product_group": None,
        "utm_source": None, "dispatcher": "Иванова", "responsible": "П", "campaign_id": "114",
    }
    sessions = {"c1": [
        {"visit_ts": datetime(2026, 6, 8, 9, 0, 0), "visit_duration": 100, "bounce": 0, "page_views": 2, "device_category": "desktop", "source_engine": "ad"},
        {"visit_ts": datetime(2026, 6, 8, 10, 0, 0), "visit_duration": 100, "bounce": 0, "page_views": 2, "device_category": "desktop", "source_engine": "ad"},
        {"visit_ts": datetime(2026, 6, 9, 9, 0, 0), "visit_duration": 50, "bounce": 0, "page_views": 1, "device_category": "desktop", "source_engine": "ad"},
        {"visit_ts": datetime(2026, 6, 12, 9, 0, 0), "visit_duration": 300, "bounce": 0, "page_views": 3, "device_category": "desktop", "source_engine": "ad"},  # ПОСЛЕ заявки — не учитывать
    ]}
    rows = build_feature_rows([lead], sessions=sessions, deadlines=[date(2026, 8, 20)], today=date(2026, 7, 23))
    f = rows[0]["features"]
    assert f["f__visits_before_lead"] == 3          # 2+1 до 10-го, 12-е исключено
    assert f["f__sessions_before"] == 2             # 2 дня с визитами до заявки
    assert f["f__days_since_first_touch"] == 2      # 10 − 8
    assert f["f__had_repeat_visit"] == 1
    assert f["f__mins_to_connection"] == 30         # 14:00 − 13:30
    assert f["f__created_hour"] == 13
    assert f["f__beh_visits"] == 3                  # time-aware: только до заявки
