import os
from datetime import date, datetime
from unittest.mock import patch

import pytest

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


def _by_pub(rows):
    """Свернуть детальные строки до (неделя, партнёр) — родительский итог."""
    agg = {}
    for (w, p, _d, n) in rows:
        agg[(w, p)] = agg.get((w, p), 0) + n
    return agg


def _inst(dev, dt, pub, reinst="0", reattr="0", utm=None):
    r = {"appmetrica_device_id": dev, "install_datetime": dt, "publisher_name": pub,
         "is_reinstallation": reinst, "is_reattribution": reattr}
    if utm:
        r["click_url_parameters"] = "utm_source=" + utm
    return r


def test_build_installs_weekly_counts_unique_devices_per_week():
    installs = [
        _inst("d1", "2026-01-06 10:00:00", "VK Ads"),      # пн 05.01
        _inst("d2", "2026-01-08 10:00:00", "VK Ads"),      # та же неделя
        _inst("d1", "2026-01-09 11:00:00", "VK Ads"),      # то же устройство в той же неделе
        _inst("d3", "2026-01-06 10:00:00", "Organic"),
    ]
    rows = _by_pub(m.build_installs_weekly(installs, False, False))
    assert rows[(date(2026, 1, 5), "VK Ads")] == 2        # d1 не задваивается
    assert rows[(date(2026, 1, 5), "Organic")] == 1


def test_build_installs_weekly_counts_device_again_in_a_later_week():
    """Ключевое отличие от когорт: повторная установка в другой неделе ДОЛЖНА считаться.
    Глобальный дедуп по первой установке терял такие устройства в свежих неделях."""
    installs = [
        _inst("d1", "2026-01-06 10:00:00", "VK Ads"),
        _inst("d1", "2026-02-10 10:00:00", "VK Ads"),     # то же устройство, другая неделя
    ]
    rows = _by_pub(m.build_installs_weekly(installs, False, False))
    assert rows[(date(2026, 1, 5), "VK Ads")] == 1
    assert rows[(date(2026, 2, 9), "VK Ads")] == 1        # не потеряно


def test_build_installs_weekly_respects_filters():
    installs = [
        _inst("d1", "2026-01-06 10:00:00", "VK Ads", reattr="true"),
        _inst("d2", "2026-01-06 10:00:00", "VK Ads", reinst="true"),
        _inst("d3", "2026-01-06 10:00:00", "VK Ads"),
    ]
    rows = _by_pub(m.build_installs_weekly(installs, False, False))
    assert rows[(date(2026, 1, 5), "VK Ads")] == 1        # остаётся только d3
    rows_keep = _by_pub(m.build_installs_weekly(installs, True, True))
    assert rows_keep[(date(2026, 1, 5), "VK Ads")] == 3


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
    # d1: покупка ДО установки (должна быть проигнорирована guard'ом lm < 0) +
    # настоящая покупка ВНУТРИ окна (life 1). Если guard убрать — first_life
    # защёлкнется на отрицательном месяце и d1 исчезнет из всех бакетов;
    # с guard'ом d1 должен считаться начиная с life_month своей ВАЛИДНОЙ покупки.
    fi = {"d1": {"install_dt": datetime(2026, 2, 1), "publisher": "VK Ads"}}
    purchases = [
        {"appmetrica_device_id": "d1", "event_datetime": "2026-01-01 10:00:00"},  # до установки — игнор
        {"appmetrica_device_id": "d1", "event_datetime": "2026-03-15 10:00:00"},  # life 1 — валидна
    ]
    rows = {(cm, p, lm): (sz, b) for (cm, p, lm, sz, b) in m.build_cohorts(fi, purchases, max_life=2)}
    assert rows[(date(2026, 2, 1), "VK Ads", 0)] == (1, 0)   # к M0 покупок ещё нет
    assert rows[(date(2026, 2, 1), "VK Ads", 1)] == (1, 1)   # к M1 — валидная покупка учтена
    assert rows[(date(2026, 2, 1), "VK Ads", 2)] == (1, 1)   # накопительно держится


def test_window_months_back():
    since, until = m.sync_window(months=7, today=date(2026, 7, 18))
    assert since == "2026-01-01"      # 6 полных месяцев назад от начала июля + текущий
    assert until == "2026-07-18"


def test_window_months_back_crosses_year_boundary():
    # today=15.02.2026, months=7: first=01.02.2026, mo=2-6=-4 → нормализация
    # (mo+=12→8, y-=1→2025) → since=2025-08-01. 7 месяцев включительно:
    # авг,сен,окт,ноя,дек,янв,фев.
    since, until = m.sync_window(months=7, today=date(2026, 2, 15))
    assert since == "2025-08-01"
    assert until == "2026-02-15"


def test_build_cohorts_excludes_devices_whose_first_purchase_is_beyond_window():
    # d1 покупает внутри окна (life 2), d2 покупает только далеко за пределами
    # окна (life ~9 при install в январе, purchase в октябре, max_life=3).
    # d2 не должен попасть НИ В ОДИН бакет, включая max_life — клэмп min(lm, max_life)
    # раздувал бы финальный (max_life) столбец, по которому сверяют с UI AppMetrica.
    fi = {
        "d1": {"install_dt": datetime(2026, 1, 5), "publisher": "VK Ads"},
        "d2": {"install_dt": datetime(2026, 1, 10), "publisher": "VK Ads"},
    }
    purchases = [
        {"appmetrica_device_id": "d1", "event_datetime": "2026-03-15 10:00:00"},  # life 2 — внутри окна
        {"appmetrica_device_id": "d2", "event_datetime": "2026-10-01 10:00:00"},  # life 9 — за окном
    ]
    rows = {(cm, p, lm): (sz, b) for (cm, p, lm, sz, b) in m.build_cohorts(fi, purchases, max_life=3)}
    cm, pub = date(2026, 1, 1), "VK Ads"
    assert rows[(cm, pub, 0)] == (2, 0)
    assert rows[(cm, pub, 1)] == (2, 0)
    assert rows[(cm, pub, 2)] == (2, 1)   # только d1
    assert rows[(cm, pub, 3)] == (2, 1)   # d2 не попадает даже в финальный (max_life) бакет
    # cohort_size остаётся 2 — оба устройства установили приложение, просто d2 не купил в окне.


def test_sync_refuses_to_wipe_when_installs_raw_is_empty():
    # Пустой ответ Logs API (транзиентный сбой / неверный APPMETRICA_APP_ID / неверное
    # окно дат) — это НЕ ошибка на уровне HTTP, просто [] (fetch_installations.json()
    # .get("data", [])). Если это молча проходит в _write — DELETE отработает,
    # INSERT вставит 0 строк, витрина обнулится до следующего успешного запуска.
    with patch.dict(os.environ, {"APPMETRICA_TOKEN": "test-token"}, clear=False), \
         patch("sync.lime_appmetrica.fetch_installations", return_value=[]), \
         patch("sync.lime_appmetrica.fetch_purchase_events", return_value=[]), \
         patch("sync.lime_appmetrica._write") as mock_write:
        with pytest.raises(RuntimeError):
            m.sync_lime_appmetrica()
    mock_write.assert_not_called()   # ключевая проверка: без неё тест прошёл бы даже при wipe


def test_build_installs_weekly_splits_by_utm_source():
    installs = [
        _inst("d1", "2026-01-06 10:00:00", "Website", utm="ya.direct"),
        _inst("d2", "2026-01-07 10:00:00", "Website", utm="google"),
        _inst("d3", "2026-01-08 10:00:00", "Website"),          # без параметров
    ]
    rows = {(w, p, d): n for (w, p, d, n) in m.build_installs_weekly(installs, False, False)}
    wk = date(2026, 1, 5)
    assert rows[(wk, "Website", "ya.direct")] == 1
    assert rows[(wk, "Website", "google")] == 1
    assert rows[(wk, "Website", "")] == 1


def test_details_sum_exactly_to_parent_when_device_switches_utm():
    """Устройство с двумя установками в неделе под разными utm не должно задваиваться:
    за ним закрепляется utm самой ранней установки, иначе детали не сложатся в родителя."""
    installs = [
        _inst("d1", "2026-01-06 10:00:00", "Website", utm="google"),   # раньше
        _inst("d1", "2026-01-09 10:00:00", "Website", utm="ig"),       # позже, то же устройство
        _inst("d2", "2026-01-07 10:00:00", "Website", utm="ig"),
    ]
    rows = m.build_installs_weekly(installs, False, False)
    wk = date(2026, 1, 5)
    by_detail = {d: n for (w, p, d, n) in rows}
    assert by_detail["google"] == 1        # d1 закреплён за ранним utm
    assert by_detail["ig"] == 1            # только d2
    assert _by_pub(rows)[(wk, "Website")] == 2   # детали == родитель, без задвоения
