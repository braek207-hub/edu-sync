# -*- coding: utf-8 -*-
"""Э3.7 (запись): запрет площадок сети.

Рычаг-близнец минус-фраз (writer/negatives.py): тот же накопительный список,
заменяемый в API целиком, тот же кап такта, та же опасность промаха —
отсечённый трафик не вернуть. Отличаются лимиты кабинета (1000 площадок по
255 символов) и то, что площадка не разбирается на слова.
"""

from sync.agent.writer.placements import (
    MAX_ZERO_SITES_PER_TICK,
    MAX_EXCLUDED_SITES,
    MAX_SITE_CHARS,
    MAX_SITES_PER_TICK,
    PLACEMENT_KIND,
    candidates_from_computed,
    computed_rows,
    diff_placements,
    merge_sites,
    plan_placements,
    site_is_valid,
)


def _candidate(placement, cost=30_000.0, campaigns=("1",)):
    return {"placement": placement, "cost": cost, "clicks": 300,
            "conversions": 0, "cpa": cost / 3, "reason": "zero_conversions",
            "campaigns": list(campaigns)}


def test_site_validation_rejects_junk():
    assert site_is_valid("some.site.ru")[0]
    assert not site_is_valid("")[0]
    assert not site_is_valid("я" * (MAX_SITE_CHARS + 1))[0]
    # Пробелы в имени площадки означают, что это не домен и не bundle id.
    assert not site_is_valid("две площадки")[0]


def test_measured_zero_sites_get_the_wide_cap():
    # Запрет измеренного нуля — арифметика (класс 0, терять нечего), и хвост
    # из сотен дешёвых нулевых площадок не должен ждать месяцами за тесным
    # капом наблюдаемых действий (решение Павла 02.09.2026).
    plan = plan_placements([_candidate(f"site{i}.ru", cost=1000.0 * i)
                            for i in range(1, MAX_ZERO_SITES_PER_TICK + 4)])
    assert len(plan["desired"]["1"]) == MAX_ZERO_SITES_PER_TICK
    assert f"site{MAX_ZERO_SITES_PER_TICK + 3}.ru" in plan["desired"]["1"]
    assert plan["over_cap"] == 3


def test_sites_costing_conversions_keep_the_tight_cap():
    # Площадка с конверсиями (и с неизмеренными конверсиями) при отсечении
    # стоит лидов — такт обязан оставаться различимым в наблюдении.
    with_conversions = [
        {**_candidate(f"conv{i}.ru", cost=1000.0 * i),
         "conversions": 2, "reason": "cpa_above_limit"}
        for i in range(1, MAX_SITES_PER_TICK + 3)]
    unmeasured = [
        {**_candidate(f"unknown{i}.ru", cost=500.0), "conversions": None}
        for i in range(3)]
    plan = plan_placements(with_conversions + unmeasured)
    assert len(plan["desired"]["1"]) == MAX_SITES_PER_TICK
    # Самые дорогие — первыми: неизмеренные дешёвые не вытесняют дорогих.
    assert f"conv{MAX_SITES_PER_TICK + 2}.ru" in plan["desired"]["1"]
    assert plan["over_cap"] == 5


def test_merge_keeps_existing_and_respects_the_cabinet_limit():
    existing = [f"old{i}.ru" for i in range(MAX_EXCLUDED_SITES)]
    merged = merge_sites(existing, ["new.ru"])
    assert len(merged) == MAX_EXCLUDED_SITES
    assert "new.ru" not in merged          # места нет — прежний список цел


def test_action_carries_union_and_previous_state():
    actions, refused = diff_placements(
        {"1": ["trash.site"]},
        {"1": {"excluded_sites": ["old.site"], "campaign_type": "TEXT_CAMPAIGN"}})
    assert not refused
    action = actions[0]
    assert action["action_kind"] == PLACEMENT_KIND
    assert action["payload"]["ExcludedSites"]["Items"] == ["old.site", "trash.site"]
    assert action["previous_state"]["ExcludedSites"]["Items"] == ["old.site"]
    assert action["payload"]["AddedSites"] == ["trash.site"]


def test_no_action_when_site_already_excluded():
    actions, refused = diff_placements(
        {"1": ["trash.site"]},
        {"1": {"excluded_sites": ["trash.site"], "campaign_type": "TEXT_CAMPAIGN"}})
    assert not actions and not refused


def test_computed_bridge_round_trips():
    rows = computed_rows([_candidate("trash.site", campaigns=("1", "2"))])
    assert rows["1"][0]["setting_kind"] == "excluded_site"
    back = candidates_from_computed(rows)
    assert back[0]["placement"] == "trash.site"
    assert sorted(back[0]["campaigns"]) == ["1", "2"]
    assert back[0]["cost"] == 60_000.0      # расход площадки по обеим кампаниям


# ------------------- конверсии площадки доезжают через витрину


def test_placement_conversions_survive_the_round_trip_through_computed():
    """Уберите этот тест — и площадка с дорогими конверсиями станет «класс 0».

    Тот же дефект, что у минус-фраз: витрина несла только клики, а обратная
    сборка подставляла conversions=0. Кандидат cpa_above_limit (конверсии
    есть, но дороже допустимого) выглядел утверждением о прошлом «конверсий
    нет» и получал право резаться без риск-бюджета.
    """
    from sync.agent.writer.placements import PLACEMENT_CONVERSIONS_KIND
    rows = computed_rows([{
        "placement": "trash.site", "cost": 30_000.0, "clicks": 300,
        "conversions": 3, "reason": "cpa_above_limit", "campaigns": ["1"],
        "cost_by_campaign": {"1": 30_000.0},
        "conversions_by_campaign": {"1": 3},
    }])
    assert {r["setting_kind"] for r in rows["1"]} == {"excluded_site",
                                                     PLACEMENT_CONVERSIONS_KIND}
    candidate = candidates_from_computed(rows)[0]
    assert candidate["conversions"] == 3.0


def test_old_placement_row_means_conversions_unknown():
    # Строка витрины до 27.08.2026 конверсий не несёт. Ноль вместо «неизвестно»
    # раздал бы бесплатные запреты всему, что успело накопиться.
    computed = {"1": [{"setting_kind": "excluded_site",
                       "setting_key": "trash.site", "value": 30_000.0,
                       "raw_value": 300, "support_n": 300}]}
    candidate = candidates_from_computed(computed)[0]
    assert candidate["conversions"] is None
    plan = plan_placements([candidate])
    assert plan["unknown_conversions"] == ["1"]
    assert plan["cut_conversions"] == {}


# ------------------- основание класса 0 производит сам рычаг


def test_placement_ban_carries_its_evidence():
    """Тот же дефект, что у минус-фраз: evidence не производил никто.

    Без него запрет площадки, вырезающий трафик с нулём конверсий на трёх CPA,
    приезжает в отбор классом 2 — то есть платит риском и стоит в очереди
    позади корректировок.
    """
    from sync.agent.writer import tier
    plan = plan_placements([{
        "placement": "trash.site", "cost": 90_000.0, "clicks": 300,
        "conversions": 0.0, "reason": "zero_conversions", "campaigns": ["1"],
        "cost_by_campaign": {"1": 90_000.0},
        "conversions_by_campaign": {"1": 0.0},
    }])
    actions, _ = diff_placements(
        plan["desired"], {"1": {"excluded_sites": [],
                                "campaign_type": "TEXT_CAMPAIGN"}},
        cut_cost=plan["cut_cost"], cut_conversions=plan["cut_conversions"],
        baseline_cpa=2_400.0)
    assert tier.tier_of(actions[0]) == tier.TIER_ARITHMETIC
    assert actions[0]["evidence"]["cost_rub"] == 90_000.0
