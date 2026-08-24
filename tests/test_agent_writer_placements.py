# -*- coding: utf-8 -*-
"""Э3.7 (запись): запрет площадок сети.

Рычаг-близнец минус-фраз (writer/negatives.py): тот же накопительный список,
заменяемый в API целиком, тот же кап такта, та же опасность промаха —
отсечённый трафик не вернуть. Отличаются лимиты кабинета (1000 площадок по
255 символов) и то, что площадка не разбирается на слова.
"""

from sync.agent.writer.placements import (
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


def test_plan_caps_sites_per_tick_by_cost():
    plan = plan_placements([_candidate(f"site{i}.ru", cost=1000.0 * i)
                            for i in range(1, MAX_SITES_PER_TICK + 4)])
    assert len(plan["desired"]["1"]) == MAX_SITES_PER_TICK
    assert f"site{MAX_SITES_PER_TICK + 3}.ru" in plan["desired"]["1"]
    assert plan["over_cap"] == 3


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
