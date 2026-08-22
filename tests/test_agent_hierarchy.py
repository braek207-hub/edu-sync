# -*- coding: utf-8 -*-
from sync.agent.computed import NO_CLICKS_REASON, NO_CONVERSIONS_REASON
from sync.agent.hierarchy import hierarchical_modifiers, shrink_toward


def _row(campaign_id, slice_key, clicks, conversions):
    return {"campaign_id": campaign_id, "slice_key": slice_key,
            "clicks": clicks, "conversions": conversions}


DIRECTIONS = {"c1": "spo", "c2": "spo", "c3": "dist"}


# ------------------------------------------------------------- shrink_toward

def test_shrink_moves_toward_target_when_data_thin():
    # 10 кликов при базе 5 % — почти весь вес у родителя.
    est = shrink_toward(obs_conv=0.5, n=10, target_conv=0.05, prior_base_conv=0.05)
    assert est < 0.2


def test_shrink_keeps_observation_when_data_rich():
    est = shrink_toward(obs_conv=0.5, n=100000, target_conv=0.05, prior_base_conv=0.05)
    assert est > 0.45


def test_shrink_returns_target_without_data():
    assert shrink_toward(0.9, 0, 0.07, 0.05) == 0.07


# ------------------------------------------------------------------- отказы

def test_refuses_without_clicks():
    _, reason = hierarchical_modifiers([], DIRECTIONS, "device")
    assert reason == NO_CLICKS_REASON


def test_refuses_without_conversions():
    rows = [_row("c1", "mobile", 1000, 0)]
    _, reason = hierarchical_modifiers(rows, DIRECTIONS, "device")
    assert reason == NO_CONVERSIONS_REASON


# ------------------------------------------------------------------ механика

def test_campaign_with_rich_data_keeps_own_signal():
    """Кампания с большим объёмом получает свою оценку, а не кабинетную."""
    rows = [
        # c1: мобайл конвертит вдвое лучше десктопа, данных много.
        _row("c1", "mobile", 20000, 2000), _row("c1", "desktop", 20000, 1000),
        # c2: противоположная картина, данных тоже много.
        _row("c2", "mobile", 20000, 1000), _row("c2", "desktop", 20000, 2000),
    ]
    by_campaign, reason = hierarchical_modifiers(rows, DIRECTIONS, "device")
    assert reason is None
    c1 = {r["setting_key"]: r["value"] for r in by_campaign["c1"]}
    c2 = {r["setting_key"]: r["value"] for r in by_campaign["c2"]}
    # Знаки противоположные — кабинетный расчёт дал бы обеим одно и то же.
    assert c1["mobile"] > 0 > c1["desktop"]
    assert c2["mobile"] < 0 < c2["desktop"]


def test_thin_campaign_borrows_from_direction():
    """Мало своих данных → оценка прижата к направлению, не к сырому наблюдению."""
    rows = [
        # Направление spo задаёт картину: мобайл лучше.
        _row("c1", "mobile", 30000, 3000), _row("c1", "desktop", 30000, 1500),
        # c2 (то же направление): 40 кликов, наблюдение дикое — 50 % конверсия.
        _row("c2", "mobile", 40, 20), _row("c2", "desktop", 40, 2),
    ]
    by_campaign, _ = hierarchical_modifiers(rows, DIRECTIONS, "device")
    c2 = {r["setting_key"]: r for r in by_campaign["c2"]}
    # Сырое отношение мобайла к базе c2 ≈ 1.8; после займа у направления —
    # заметно ближе к направленческому ≈ 1.33.
    assert c2["mobile"]["raw_value"] < 1.7
    # 40 кликов против приора ~27 наблюдений (2 события / базу направления)
    # — своих данных чуть больше половины, но далеко не 0.85, как давал
    # приор от собственной шумной базы.
    assert c2["mobile"]["pool_weight"] < 0.65


def test_direction_isolates_campaign_from_foreign_pattern():
    """Кампания dist занимает у dist, а не у среднего по кабинету."""
    rows = [
        # spo — огромный объём: мобайл сильно лучше.
        _row("c1", "mobile", 50000, 5000), _row("c1", "desktop", 50000, 2000),
        # dist — свой объём: мобайл ХУЖЕ.
        _row("c3", "mobile", 20000, 600), _row("c3", "desktop", 20000, 1200),
    ]
    by_campaign, _ = hierarchical_modifiers(rows, DIRECTIONS, "device")
    c3 = {r["setting_key"]: r["value"] for r in by_campaign["c3"]}
    assert c3["mobile"] < 0 < c3["desktop"]


def test_segment_below_support_not_emitted():
    rows = [
        _row("c1", "mobile", 10000, 500),
        _row("c1", "tablet", 29, 5),  # ниже MIN_SUPPORT=30
    ]
    by_campaign, _ = hierarchical_modifiers(rows, DIRECTIONS, "device")
    keys = {r["setting_key"] for r in by_campaign.get("c1", [])}
    assert "tablet" not in keys


def test_tail_key_other_is_ignored():
    rows = [
        _row("c1", "mobile", 10000, 500), _row("c1", "desktop", 10000, 300),
        _row("c1", "other", 5000, 400),
    ]
    by_campaign, _ = hierarchical_modifiers(rows, DIRECTIONS, "device")
    keys = {r["setting_key"] for r in by_campaign["c1"]}
    assert "other" not in keys


def test_campaign_without_own_conversions_gets_no_rows():
    """Ноль своих конверсий — кампания остаётся на кабинетном уровне."""
    rows = [
        _row("c1", "mobile", 10000, 500), _row("c1", "desktop", 10000, 300),
        _row("c2", "mobile", 500, 0), _row("c2", "desktop", 500, 0),
    ]
    by_campaign, _ = hierarchical_modifiers(rows, DIRECTIONS, "device")
    assert "c2" not in by_campaign


def test_rows_carry_setting_contract_of_computed():
    """Формат строк совместим с edu_agent_computed_settings."""
    rows = [_row("c1", "mobile", 10000, 500), _row("c1", "desktop", 10000, 300)]
    by_campaign, _ = hierarchical_modifiers(rows, DIRECTIONS, "device")
    row = by_campaign["c1"][0]
    assert row["setting_kind"] == "bid_modifier:device"
    assert set(row) >= {"setting_kind", "setting_key", "value",
                        "support_n", "raw_value", "pool_weight"}
