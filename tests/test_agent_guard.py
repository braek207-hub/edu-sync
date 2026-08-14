from sync.agent.guard import (
    check_continuity,
    check_freshness,
    check_sum_reconciliation,
    check_volume_anomaly,
    verdict,
)


def test_freshness_ok_when_all_recent():
    checks = check_freshness(
        {"direct_stats": "2026-08-13T04:00:00+00:00", "crm": "2026-08-13T05:00:00+00:00"},
        now_iso="2026-08-13T09:00:00+00:00",
    )
    assert all(c["status"] == "OK" for c in checks)


def test_freshness_fails_when_stale_over_36h():
    checks = check_freshness(
        {"direct_stats": "2026-08-11T04:00:00+00:00"},
        now_iso="2026-08-13T09:00:00+00:00",
    )
    assert checks[0]["status"] == "FAIL"
    assert checks[0]["detail"]["age_hours"] > 36


def test_freshness_fails_on_missing_source():
    checks = check_freshness({"crm": None}, now_iso="2026-08-13T09:00:00+00:00")
    assert checks[0]["status"] == "FAIL"


def test_volume_anomaly_ok_within_sigma():
    history = [100.0, 102.0, 98.0, 101.0, 99.0, 100.0, 103.0]
    assert check_volume_anomaly(history, today=101.0)["status"] == "OK"


def test_volume_anomaly_fails_on_collapse():
    history = [100.0, 102.0, 98.0, 101.0, 99.0, 100.0, 103.0]
    assert check_volume_anomaly(history, today=0.0)["status"] == "FAIL"


def test_volume_anomaly_ok_when_history_too_short():
    # Мало истории — проверка не должна блокировать агента ложным срабатыванием.
    assert check_volume_anomaly([100.0, 101.0], today=0.0)["status"] == "OK"


def test_volume_anomaly_ok_when_history_is_flat():
    # Нулевая дисперсия: не делить на ноль и не падать в FAIL на любом отклонении.
    assert check_volume_anomaly([100.0] * 7, today=100.0)["status"] == "OK"


def test_sum_reconciliation_ok_within_tolerance():
    assert check_sum_reconciliation(1_000_000.0, 1_005_000.0)["status"] == "OK"


def test_sum_reconciliation_fails_over_one_percent():
    assert check_sum_reconciliation(1_000_000.0, 1_100_000.0)["status"] == "FAIL"


def test_sum_reconciliation_ok_when_both_zero():
    assert check_sum_reconciliation(0.0, 0.0)["status"] == "OK"


def test_continuity_fails_on_gap():
    dates = ["2026-08-10", "2026-08-11", "2026-08-13"]
    assert check_continuity(dates, expected_last="2026-08-13")["status"] == "FAIL"


def test_continuity_ok_without_gap():
    dates = ["2026-08-11", "2026-08-12", "2026-08-13"]
    assert check_continuity(dates, expected_last="2026-08-13")["status"] == "OK"


def test_verdict_red_if_any_fail():
    checks = [{"check_name": "a", "status": "OK", "detail": {}},
              {"check_name": "b", "status": "FAIL", "detail": {}}]
    assert verdict(checks) == "RED"


def test_verdict_green_when_all_ok():
    checks = [{"check_name": "a", "status": "OK", "detail": {}}]
    assert verdict(checks) == "GREEN"
