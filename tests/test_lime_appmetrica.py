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


def _buy(dev, ym, amount=1000.0, txn=None):
    """Факт покупки: (device, месяц покупки, transaction_id, сумма)."""
    y, mo = ym
    return (dev, date(y, mo, 1), txn or f"{dev}-{y}{mo:02d}", amount)


def _by_pub(rows):
    """Свернуть детальные строки до (день, партнёр) — родительский итог."""
    agg = {}
    for (d, p, _det, _camp, n) in rows:
        agg[(d, p)] = agg.get((d, p), 0) + n
    return agg


def _inst(dev, dt, pub, reinst="0", reattr="0", utm=None, camp=None):
    r = {"appmetrica_device_id": dev, "install_datetime": dt, "publisher_name": pub,
         "is_reinstallation": reinst, "is_reattribution": reattr}
    parts = []
    if utm:
        parts.append("utm_source=" + utm)
    if camp:
        parts.append("campaign_id=" + camp)
    if parts:
        r["click_url_parameters"] = "&".join(parts)
    return r


def test_build_installs_daily_counts_unique_devices_per_day():
    installs = [
        _inst("d1", "2026-01-06 10:00:00", "VK Ads"),
        _inst("d1", "2026-01-06 18:00:00", "VK Ads"),   # то же устройство, тот же день
        _inst("d2", "2026-01-06 10:00:00", "VK Ads"),
        _inst("d3", "2026-01-06 10:00:00", "Organic"),
    ]
    rows = _by_pub(m.build_installs_daily(installs, False, False))
    assert rows[(date(2026, 1, 6), "VK Ads")] == 2      # d1 не задваивается
    assert rows[(date(2026, 1, 6), "Organic")] == 1


def test_build_installs_daily_counts_device_again_on_another_day():
    """Повторная установка в другой день ДОЛЖНА считаться — в отличие от когорт."""
    installs = [
        _inst("d1", "2026-01-06 10:00:00", "VK Ads"),
        _inst("d1", "2026-02-10 10:00:00", "VK Ads"),
    ]
    rows = _by_pub(m.build_installs_daily(installs, False, False))
    assert rows[(date(2026, 1, 6), "VK Ads")] == 1
    assert rows[(date(2026, 2, 10), "VK Ads")] == 1


def test_build_installs_daily_respects_filters():
    installs = [
        _inst("d1", "2026-01-06 10:00:00", "VK Ads", reattr="true"),
        _inst("d2", "2026-01-06 10:00:00", "VK Ads", reinst="true"),
        _inst("d3", "2026-01-06 10:00:00", "VK Ads"),
    ]
    # переатрибуции считаем, переустановки нет
    rows = _by_pub(m.build_installs_daily(installs, True, False))
    assert rows[(date(2026, 1, 6), "VK Ads")] == 2
    rows_strict = _by_pub(m.build_installs_daily(installs, False, False))
    assert rows_strict[(date(2026, 1, 6), "VK Ads")] == 1


def test_build_installs_daily_keeps_campaign_id_for_direct():
    """campaign_id нужен, чтобы приклеить установки к строке кампании в таблице."""
    installs = [
        _inst("d1", "2026-01-06 10:00:00", "Yandex.Direct", utm="ya.direct", camp="704121835"),
        _inst("d2", "2026-01-06 10:00:00", "VK Ads", utm="vk_ads"),   # у VK кампании нет
    ]
    rows = {(p, camp): n for (_d, p, _det, camp, n) in m.build_installs_daily(installs, False, False)}
    assert rows[("Yandex.Direct", "704121835")] == 1
    assert rows[("VK Ads", "")] == 1                    # пусто, а не мусор


def test_build_cohorts_cumulative_unique_buyers():
    fi = {
        "d1": {"install_dt": datetime(2026, 1, 10), "publisher": "VK Ads"},
        "d2": {"install_dt": datetime(2026, 1, 20), "publisher": "VK Ads"},
        "d3": {"install_dt": datetime(2026, 1, 25), "publisher": "VK Ads"},
    }
    purchases = [
        _buy("d1", (2026, 1)),  # life 0
        _buy("d1", (2026, 3)),  # повтор — не двоит
        _buy("d2", (2026, 2)),  # life 1
    ]
    rows = {(cm, p, lm): (sz, b) for (cm, p, lm, sz, b, _o, _r) in m.build_cohorts(fi, purchases, max_life=3)}
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
        _buy("d1", (2026, 1)),  # до установки — игнор
        _buy("d1", (2026, 3)),  # life 1 — валидна
    ]
    rows = {(cm, p, lm): (sz, b) for (cm, p, lm, sz, b, _o, _r) in m.build_cohorts(fi, purchases, max_life=2)}
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
        _buy("d1", (2026, 3)),  # life 2 — внутри окна
        _buy("d2", (2026, 10)),  # life 9 — за окном
    ]
    rows = {(cm, p, lm): (sz, b) for (cm, p, lm, sz, b, _o, _r) in m.build_cohorts(fi, purchases, max_life=3)}
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


def test_build_installs_daily_splits_by_utm_source():
    installs = [
        _inst("d1", "2026-01-06 10:00:00", "Website", utm="ya.direct"),
        _inst("d2", "2026-01-06 10:00:00", "Website", utm="google"),
        _inst("d3", "2026-01-06 10:00:00", "Website"),          # без параметров
    ]
    rows = {(p, det): n for (_d, p, det, _c, n) in m.build_installs_daily(installs, False, False)}
    assert rows[("Website", "ya.direct")] == 1
    assert rows[("Website", "google")] == 1
    assert rows[("Website", "")] == 1


def test_details_sum_exactly_to_parent_when_device_switches_utm():
    """Устройство с двумя установками в дне под разными utm не должно задваиваться:
    за ним закрепляется utm самой ранней установки, иначе детали не сложатся в родителя."""
    installs = [
        _inst("d1", "2026-01-06 10:00:00", "Website", utm="google"),   # раньше
        _inst("d1", "2026-01-06 18:00:00", "Website", utm="ig"),       # позже, то же устройство
        _inst("d2", "2026-01-06 12:00:00", "Website", utm="ig"),
    ]
    rows = m.build_installs_daily(installs, False, False)
    by_detail = {det: n for (_d, _p, det, _c, n) in rows}
    assert by_detail["google"] == 1        # d1 закреплён за ранним utm
    assert by_detail["ig"] == 1            # только d2
    assert _by_pub(rows)[(date(2026, 1, 6), "Website")] == 2   # детали == родитель


def test_purchase_facts_dedups_by_transaction_id():
    """Одно событие иногда приходит дважды — без дедупа задвоились бы заказы и выручка."""
    events = [
        {"appmetrica_device_id": "d1", "event_datetime": "2026-01-10 10:00:00",
         "event_json": '{"transaction_id": 111, "value": 4599}'},
        {"appmetrica_device_id": "d1", "event_datetime": "2026-01-10 10:00:05",
         "event_json": '{"transaction_id": 111, "value": 4599}'},   # дубль
        {"appmetrica_device_id": "d1", "event_datetime": "2026-01-20 10:00:00",
         "event_json": '{"transaction_id": 222, "value": 1000}'},
    ]
    facts = m.purchase_facts(events)
    assert len(facts) == 2                       # дубль отброшен
    assert sum(f[3] for f in facts) == 5599.0    # выручка не задвоена


def test_purchase_facts_survives_broken_json():
    events = [
        {"appmetrica_device_id": "d1", "event_datetime": "2026-01-10 10:00:00",
         "event_json": "не json"},
        {"appmetrica_device_id": "d2", "event_datetime": "2026-01-10 10:00:00"},
    ]
    facts = m.purchase_facts(events)
    assert len(facts) == 2                       # события не теряем
    assert all(f[3] == 0.0 for f in facts)       # сумма неизвестна → 0


def test_build_cohorts_orders_and_revenue_are_cumulative():
    """Покупатель считается один раз, заказы и выручка — каждый раз."""
    fi = {"d1": {"install_dt": datetime(2026, 1, 5), "publisher": "VK Ads"}}
    purchases = [
        _buy("d1", (2026, 1), 1000.0, "t1"),
        _buy("d1", (2026, 2), 500.0, "t2"),
        _buy("d1", (2026, 2), 300.0, "t3"),
    ]
    rows = {lm: (sz, b, o, r) for (cm, p, lm, sz, b, o, r) in m.build_cohorts(fi, purchases, 2)}
    assert rows[0] == (1, 1, 1, 1000.0)          # M0: 1 покупатель, 1 заказ, 1000
    assert rows[1] == (1, 1, 3, 1800.0)          # M1: покупатель тот же, заказов уже 3
    assert rows[2] == (1, 1, 3, 1800.0)          # M2: новых нет, накопленное держится


def test_month_chunks_splits_window_by_calendar_months():
    ch = m.month_chunks("2026-01-15", "2026-03-10")
    assert ch == [("2026-01-15", "2026-01-31"),
                  ("2026-02-01", "2026-02-28"),
                  ("2026-03-01", "2026-03-10")]


def test_daily_cohort_attributes_money_to_first_install_not_every_install():
    """Устройство переустановило приложение — установка считается в оба дня,
    но деньги приписываются только первому дню, иначе выручка задвоится."""
    installs = [
        _inst("d1", "2026-01-06 10:00:00", "VK Ads"),
        _inst("d1", "2026-02-10 10:00:00", "VK Ads"),   # переустановка
    ]
    purchases = [_buy("d1", (2026, 3), 5000.0, "t1")]
    rows = {(d, p): (n, o, rev)
            for (d, p, _det, _c, n, o, rev) in
            m.build_installs_daily_with_cohort(installs, purchases, False, False)}
    assert rows[(date(2026, 1, 6), "VK Ads")] == (1, 1, 5000.0)   # деньги здесь
    assert rows[(date(2026, 2, 10), "VK Ads")] == (1, 0, 0.0)     # установка есть, денег нет


def test_daily_cohort_sums_lifetime_revenue_per_install_day():
    installs = [
        _inst("d1", "2026-01-06 10:00:00", "Yandex.Direct", camp="704121835"),
        _inst("d2", "2026-01-06 11:00:00", "Yandex.Direct", camp="704121835"),
    ]
    purchases = [
        _buy("d1", (2026, 1), 1000.0, "t1"),
        _buy("d1", (2026, 5), 2000.0, "t2"),   # покупка сильно позже — всё равно этот день
        _buy("d2", (2026, 2), 3000.0, "t3"),
    ]
    rows = m.build_installs_daily_with_cohort(installs, purchases, False, False)
    assert len(rows) == 1
    (_d, pub, _det, camp, n, orders, revenue) = rows[0]
    assert (pub, camp, n) == ("Yandex.Direct", "704121835", 2)
    assert orders == 3
    assert revenue == 6000.0
