# -*- coding: utf-8 -*-
"""Э2.2b: качество лида сегмента поверх конверсии клик→лид.

Сценарий из боя (probe 32622086445): по конверсии в лид планшеты могут выглядеть
лучше и получать плюс, а по деньгам планшетный лид вдвое дешевле ПК-шного и
хуже соединяется. Корректировка обязана видеть обе компоненты.
"""

from sync.agent.segment_quality import (
    MIN_BRIDGE_LEADS,
    NO_BRIDGE_REASON,
    apply_quality_to_modifiers,
    device_quality_ratios,
)


def _leads(device, n, eff=1.0, conn=1.0, deal=1.0, paid=0.0, check=100000.0):
    """n лидов устройства с долями прохождения ступеней (кумулятивные доли от n)."""
    rows = []
    n_eff, n_conn = int(n * eff), int(n * eff * conn)
    n_deal, n_paid = int(n * eff * conn * deal), int(n * eff * conn * deal * paid)
    for i in range(n):
        rows.append({
            "device": device,
            "is_eff": i < n_eff,
            "is_connected": i < n_conn,
            "is_deal": i < n_deal,
            "is_paid": i < n_paid,
            "amount": check if i < n_paid else None,
        })
    return rows


def _bridge_pc_good_tablet_bad():
    # ПК: соединение и оплаты хорошие. Планшеты: соединение вдвое хуже, оплат
    # мало — их глубокая ступень не набирает 25 событий, лестница обязана
    # подняться выше и всё равно увидеть разницу.
    return (
        _leads("ПК", 700, eff=0.9, conn=0.5, deal=0.5, paid=0.5)
        + _leads("Смартфоны", 800, eff=0.9, conn=0.4, deal=0.5, paid=0.4)
        + _leads("Планшеты", 400, eff=0.9, conn=0.25, deal=0.5, paid=0.2)
    )


def test_small_bridge_refuses_with_reason():
    ratios, reason = device_quality_ratios(_leads("ПК", MIN_BRIDGE_LEADS - 1))
    assert ratios == {}
    assert reason == NO_BRIDGE_REASON


def test_quality_orders_devices_by_expected_revenue_per_lead():
    ratios, reason = device_quality_ratios(_bridge_pc_good_tablet_bad())
    assert reason is None
    assert ratios["DESKTOP"]["ratio"] > 1.0 > ratios["TABLET"]["ratio"]
    assert ratios["TABLET"]["ratio"] < ratios["MOBILE"]["ratio"]


def test_thin_device_climbs_the_ladder_instead_of_trusting_rare_payments():
    ratios, _ = device_quality_ratios(_bridge_pc_good_tablet_bad())
    # У планшетов оплат меньше 25 — ступень не "paid", но оценка есть.
    assert ratios["TABLET"]["step"] != "paid"
    assert ratios["TABLET"]["ratio"] > 0


def test_keys_are_direct_segment_keys_not_metrika_categories():
    ratios, _ = device_quality_ratios(_bridge_pc_good_tablet_bad())
    assert set(ratios) == {"DESKTOP", "MOBILE", "TABLET"}


def test_apply_multiplies_conversion_by_quality_and_keeps_both_visible():
    rows = [{"setting_kind": "bid_modifier:device", "setting_key": "TABLET",
             "value": 38.0, "raw_value": 1.38, "support_n": 500}]
    adjusted = apply_quality_to_modifiers(
        rows, {"TABLET": {"ratio": 0.5}})
    assert adjusted == 1
    row = rows[0]
    assert row["conv_ratio"] == 1.38
    assert row["quality_ratio"] == 0.5
    assert row["raw_value"] == 0.69          # 1.38 × 0.5
    assert row["value"] == -31.0             # 0.69 → −31 %
    # Плюс по конверсии в лид превратился в минус по деньгам — суть Э2.2b.


def test_apply_skips_other_kinds_and_unknown_segments():
    rows = [
        {"setting_kind": "bid_modifier:gender", "setting_key": "GENDER_FEMALE",
         "value": 10.0, "raw_value": 1.1},
        {"setting_kind": "bid_modifier:device", "setting_key": "SMART_TV",
         "value": -20.0, "raw_value": 0.8},
    ]
    adjusted = apply_quality_to_modifiers(rows, {"DESKTOP": {"ratio": 1.2}})
    assert adjusted == 0
    assert rows[0]["value"] == 10.0 and rows[1]["value"] == -20.0
    assert "quality_ratio" not in rows[0] and "quality_ratio" not in rows[1]


def test_bridge_without_payments_refuses():
    ratios, reason = device_quality_ratios(_leads("ПК", 2000, paid=0.0))
    assert ratios == {}
    assert "оплат" in reason
