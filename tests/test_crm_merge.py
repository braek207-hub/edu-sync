import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# googleapiclient optional locally
import unittest.mock
sys.modules.setdefault("googleapiclient", unittest.mock.MagicMock())
sys.modules.setdefault("googleapiclient.discovery", unittest.mock.MagicMock())

from sync.crm import _sync_leads_raw, merge_leads_agg, merge_payments_agg


def test_merge_leads_agg_sums_counters():
    target = {
        "2025-01-01|123|rf|unknown|unknown|unknown": {
            "date": "2025-01-01",
            "campaign_id": "123",
            "city_ip_segment": "rf",
            "b24_grad_year": "unknown",
            "b24_edu_level": "unknown",
            "audience": "unknown",
            "leads": 2,
            "eff_leads": 2,
            "connections": 1.0,
            "deals": 0.0,
            "payments_from_leads": 1,
            "days_to_pay_sum": 0.0,
            "days_to_pay_count": 0,
            "project": "unknown",
            "direction": "other",
            "campaign_name": "",
        }
    }
    source = {
        "2025-01-01|123|rf|unknown|unknown|unknown": {
            "date": "2025-01-01",
            "campaign_id": "123",
            "city_ip_segment": "rf",
            "b24_grad_year": "unknown",
            "b24_edu_level": "unknown",
            "audience": "unknown",
            "leads": 3,
            "eff_leads": 2,
            "connections": 2.0,
            "deals": 1.0,
            "payments_from_leads": 2,
            "days_to_pay_sum": 10.0,
            "days_to_pay_count": 2,
            "project": "vse",
            "direction": "spo",
            "campaign_name": "vse_spo_msk",
        },
        "2025-01-02|456|rf|unknown|unknown|unknown": {
            "date": "2025-01-02",
            "campaign_id": "456",
            "city_ip_segment": "rf",
            "b24_grad_year": "unknown",
            "b24_edu_level": "unknown",
            "audience": "unknown",
            "leads": 1,
            "eff_leads": 1,
            "connections": 0.0,
            "deals": 0.0,
            "payments_from_leads": 0,
            "days_to_pay_sum": 0.0,
            "days_to_pay_count": 0,
            "project": "vuz",
            "direction": "vpo",
            "campaign_name": "",
        },
    }
    merge_leads_agg(target, source)
    row = target["2025-01-01|123|rf|unknown|unknown|unknown"]
    assert row["leads"] == 5
    assert row["eff_leads"] == 4
    assert row["connections"] == 3.0
    assert row["deals"] == 1.0
    assert row["payments_from_leads"] == 3
    assert row["days_to_pay_sum"] == 10.0
    assert row["days_to_pay_count"] == 2
    assert row["project"] == "vse"
    assert row["direction"] == "spo"
    assert row["campaign_name"] == "vse_spo_msk"
    assert "2025-01-02|456|rf|unknown|unknown|unknown" in target


def test_merge_payments_agg_sums_revenue():
    target = {
        "2025-03-01|123|rf|unknown|unknown": {
            "date": "2025-03-01",
            "campaign_id": "123",
            "city_ip_segment": "rf",
            "b24_grad_year": "unknown",
            "b24_edu_level": "unknown",
            "payments": 1,
            "revenue": 1000.0,
            "project": "unknown",
            "direction": "other",
        }
    }
    source = {
        "2025-03-01|123|rf|unknown|unknown": {
            "date": "2025-03-01",
            "campaign_id": "123",
            "city_ip_segment": "rf",
            "b24_grad_year": "unknown",
            "b24_edu_level": "unknown",
            "payments": 2,
            "revenue": 500.0,
            "project": "provuz",
            "direction": "spo",
        }
    }
    merge_payments_agg(target, source)
    row = target["2025-03-01|123|rf|unknown|unknown"]
    assert row["payments"] == 3
    assert row["revenue"] == 1500.0
    assert row["project"] == "provuz"
    # crm_payments keys remain 5-part (no audience column in payments)


# ── (a) eff_leads excludes junk statuses, keeps real ones ──────────────────

def test_eff_leads_excludes_junk_statuses():
    """Junk-статусы (дубл/спам/тест/ошибк/повтор) не входят в eff_leads."""
    headers = ["ID", "Дата создания", "Ленд", "UTM Campaign", "Этап"]
    values = [
        headers,
        ["1", "01.03.2026", "vuz", "99999", ""],           # нет статуса → eff
        ["2", "01.03.2026", "vuz", "99999", "Дубликат"],   # junk: дубл
        ["3", "01.03.2026", "vuz", "99999", "Спам"],        # junk: спам
        ["4", "01.03.2026", "vuz", "99999", "Тест звонка"], # junk: тест
        ["5", "01.03.2026", "vuz", "99999", "Ошибка"],      # junk: ошибк
        ["6", "01.03.2026", "vuz", "99999", "Повтор"],      # junk: повтор
        ["7", "01.03.2026", "vuz", "99999", "Новый"],       # реальный
        ["8", "01.03.2026", "vuz", "99999", "В работе"],    # реальный
    ]
    agg, _, _ = _sync_leads_raw(headers, values)
    key = "2026-03-01|99999|rf|unknown|unknown|unknown"
    row = agg[key]
    assert row["leads"] == 8       # все 8 строк считаются
    assert row["eff_leads"] == 3   # ID 1 (пустой), 7, 8 — не junk


def test_eff_leads_all_real_when_no_status_column():
    """Без колонки Этап все лиды считаются эффективными."""
    headers = ["ID", "Дата создания", "Ленд", "UTM Campaign"]
    values = [
        headers,
        ["1", "05.03.2026", "vuz", "111"],
        ["2", "05.03.2026", "vuz", "111"],
    ]
    agg, _, _ = _sync_leads_raw(headers, values)
    key = "2026-03-05|111|rf|unknown|unknown|unknown"
    assert agg[key]["leads"] == 2
    assert agg[key]["eff_leads"] == 2


# ── (b) audience keying splits parent/pupil into separate rows ──────────────

def test_audience_keying_splits_parent_and_pupil():
    """«Родитель» и «Школьник» попадают в разные ключи — разные строки аггрегата."""
    headers = ["ID", "Дата создания", "Ленд", "UTM Campaign", "Родитель"]
    values = [
        headers,
        ["1", "10.03.2026", "vuz", "55555", "Родитель"],
        ["2", "10.03.2026", "vuz", "55555", "Школьник"],
        ["3", "10.03.2026", "vuz", "55555", "Родитель"],
        ["4", "10.03.2026", "vuz", "55555", ""],           # unknown
    ]
    agg, _, _ = _sync_leads_raw(headers, values)
    key_parent  = "2026-03-10|55555|rf|unknown|unknown|parent"
    key_pupil   = "2026-03-10|55555|rf|unknown|unknown|pupil"
    key_unknown = "2026-03-10|55555|rf|unknown|unknown|unknown"
    assert agg[key_parent]["leads"] == 2
    assert agg[key_pupil]["leads"] == 1
    assert agg[key_unknown]["leads"] == 1
    assert agg[key_parent]["audience"] == "parent"
    assert agg[key_pupil]["audience"] == "pupil"


def test_audience_uchenik_maps_to_pupil():
    """«Ученик» тоже нормализуется в 'pupil'."""
    headers = ["ID", "Дата создания", "Ленд", "UTM Campaign", "Аудитория"]
    values = [
        headers,
        ["1", "11.03.2026", "vuz", "77777", "Ученик"],
    ]
    agg, _, _ = _sync_leads_raw(headers, values)
    key = "2026-03-11|77777|rf|unknown|unknown|pupil"
    assert key in agg
    assert agg[key]["audience"] == "pupil"


# ── (c) days-to-pay sum/count from lead + payment with known dates ──────────

def test_days_to_pay_computed_from_paid_by_lead_id():
    """days_to_pay_sum/count вычисляются из дат создания лида и оплаты."""
    headers = ["ID", "Дата создания", "Ленд", "UTM Campaign"]
    values = [
        headers,
        ["lead-A", "01.04.2026", "vuz", "88888"],   # оплата через 5 дней
        ["lead-B", "01.04.2026", "vuz", "88888"],   # оплата через 10 дней
        ["lead-C", "01.04.2026", "vuz", "88888"],   # нет оплаты
    ]
    paid_by_lead_id = {
        "lead-A": {"count": 1, "revenue": 5000.0, "pay_date": "2026-04-06"},
        "lead-B": {"count": 1, "revenue": 3000.0, "pay_date": "2026-04-11"},
        # lead-C не в paid_by_lead_id
    }
    agg, _, _ = _sync_leads_raw(headers, values, paid_by_lead_id=paid_by_lead_id)
    key = "2026-04-01|88888|rf|unknown|unknown|unknown"
    row = agg[key]
    assert row["days_to_pay_sum"] == 15.0    # 5 + 10
    assert row["days_to_pay_count"] == 2
    assert row["payments_from_leads"] == 2
    assert row["revenue_from_leads"] == 8000.0


def test_days_to_pay_ignores_negative_days():
    """Отрицательные days (оплата раньше создания) не считаются."""
    headers = ["ID", "Дата создания", "Ленд", "UTM Campaign"]
    values = [
        headers,
        ["lead-X", "15.04.2026", "vuz", "44444"],
    ]
    paid_by_lead_id = {
        "lead-X": {"count": 1, "revenue": 1000.0, "pay_date": "2026-04-14"},  # -1 день
    }
    agg, _, _ = _sync_leads_raw(headers, values, paid_by_lead_id=paid_by_lead_id)
    key = "2026-04-15|44444|rf|unknown|unknown|unknown"
    row = agg[key]
    assert row["days_to_pay_sum"] == 0.0
    assert row["days_to_pay_count"] == 0
    # payment still counted
    assert row["payments_from_leads"] == 1
