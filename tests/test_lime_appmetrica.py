from datetime import date, datetime
from sync import lime_appmetrica as m


def test_iso_monday_and_month_helpers():
    assert m.iso_monday(datetime(2026, 1, 7)) == date(2026, 1, 5)   # среда → пн 05.01
    assert m.month_start(datetime(2026, 3, 20)) == date(2026, 3, 1)
    assert m.month_diff(date(2026, 7, 1), date(2026, 1, 1)) == 6
    assert m.month_diff(date(2026, 1, 1), date(2026, 1, 1)) == 0


def test_first_install_dedups_to_earliest():
    installs = [
        {"appmetrica_device_id": "d1", "install_datetime": "2026-02-10 09:00:00",
         "publisher_name": "VK Ads", "is_reattribution": "0", "is_reinstallation": "0"},
        {"appmetrica_device_id": "d1", "install_datetime": "2026-01-05 09:00:00",
         "publisher_name": "Organic", "is_reattribution": "0", "is_reinstallation": "0"},
    ]
    fi = m.first_install_per_device(installs, keep_reattribution=False, keep_reinstall=False)
    assert fi["d1"]["publisher"] == "Organic"                 # ранняя установка задаёт партнёра
    assert fi["d1"]["install_dt"] == datetime(2026, 1, 5, 9, 0, 0)


def test_first_install_filters_reinstall():
    installs = [
        {"appmetrica_device_id": "d1", "install_datetime": "2026-01-05 09:00:00",
         "publisher_name": "VK Ads", "is_reattribution": "0", "is_reinstallation": "1"},
    ]
    fi = m.first_install_per_device(installs, keep_reattribution=False, keep_reinstall=False)
    assert fi == {}                                           # переустановка отфильтрована


def test_build_installs_weekly_counts_devices():
    fi = {
        "d1": {"install_dt": datetime(2026, 1, 6), "publisher": "VK Ads"},   # пн 05.01
        "d2": {"install_dt": datetime(2026, 1, 8), "publisher": "VK Ads"},   # та же неделя
        "d3": {"install_dt": datetime(2026, 1, 6), "publisher": "Organic"},
    }
    rows = dict(((w, p), n) for (w, p, n) in m.build_installs_weekly(fi))
    assert rows[(date(2026, 1, 5), "VK Ads")] == 2
    assert rows[(date(2026, 1, 5), "Organic")] == 1


def test_build_cohorts_cumulative_unique_buyers():
    fi = {
        "d1": {"install_dt": datetime(2026, 1, 10), "publisher": "VK Ads"},
        "d2": {"install_dt": datetime(2026, 1, 20), "publisher": "VK Ads"},
        "d3": {"install_dt": datetime(2026, 1, 25), "publisher": "VK Ads"},
    }
    purchases = [
        {"appmetrica_device_id": "d1", "event_datetime": "2026-01-15 10:00:00"},  # life 0
        {"appmetrica_device_id": "d1", "event_datetime": "2026-03-15 10:00:00"},  # повтор — не двоит
        {"appmetrica_device_id": "d2", "event_datetime": "2026-02-05 10:00:00"},  # life 1
    ]
    rows = {(cm, p, lm): (sz, b) for (cm, p, lm, sz, b) in m.build_cohorts(fi, purchases, max_life=3)}
    cm, p = date(2026, 1, 1), "VK Ads"
    assert rows[(cm, p, 0)] == (3, 1)   # cohort_size=3, купил 1 к M0 (d1)
    assert rows[(cm, p, 1)] == (3, 2)   # накопительно к M1: d1+d2
    assert rows[(cm, p, 2)] == (3, 2)   # к M2 новых нет
    assert rows[(cm, p, 3)] == (3, 2)   # d1 повтор в марте (life0-й уже учтён) — не двоит


def test_build_cohorts_ignores_purchase_before_install():
    fi = {"d1": {"install_dt": datetime(2026, 2, 1), "publisher": "VK Ads"}}
    purchases = [{"appmetrica_device_id": "d1", "event_datetime": "2026-01-01 10:00:00"}]
    rows = {(cm, p, lm): (sz, b) for (cm, p, lm, sz, b) in m.build_cohorts(fi, purchases, max_life=2)}
    assert rows[(date(2026, 2, 1), "VK Ads", 0)] == (1, 0)   # покупка до установки игнор
