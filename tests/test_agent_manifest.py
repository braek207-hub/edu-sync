# -*- coding: utf-8 -*-
"""Манифест устройства агента (sync/agent/manifest.py).

Проверяется не содержимое отдельных чисел — они и так живут в своих модулях, —
а ПОЛНОТА: манифест обязан описывать всё, чем агент работает. Пропущенный
ключ панели или вид действия здесь означает ровно то, что уже случилось с
рукописным зеркалом в Panda-BI: человек смотрит на экран и делает вывод, что
рычага не существует.
"""

import json
import os

import pytest

from sync.agent import autonomy, config, manifest
from sync.agent.writer import guardrails, lanes, tier

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def built():
    return manifest.build("2026-08-28T00:00:00+00:00")


def test_manifest_is_json_serialisable(built):
    # Манифест едет в jsonb и оттуда в браузер: множество или dataclass,
    # просочившиеся из констант, уронили бы выгрузку в бою, а не в тесте.
    text = json.dumps(built, ensure_ascii=False)
    assert json.loads(text)["schema_version"] == manifest.SCHEMA_VERSION


def test_every_panel_key_is_described(built):
    assert {row["key"] for row in built["settings"]} == set(config.SPEC)


def test_locked_keys_are_listed_as_locked(built):
    # Порог защиты обязан быть виден именно как запертый: отсутствие ключа в
    # панели человек читает как «настройки нет», и первым делом идёт искать её
    # в коде.
    assert {row["key"] for row in built["locked"]} == set(config.LOCKED_KEYS)
    assert not ({row["key"] for row in built["locked"]}
                & {row["key"] for row in built["settings"]})


def test_locked_keys_carry_the_reason(built):
    # Без причины запертый ключ читается как недоделка экрана. Причина живёт
    # у агента и едет в манифест — панель её не сочиняет.
    for row in built["locked"]:
        assert row.get("about"), row["key"]


def test_number_settings_carry_their_range(built):
    for row in built["settings"]:
        if row["kind"] != "number":
            continue
        assert "min" in row and "max" in row, row["key"]


def test_choice_settings_carry_their_choices(built):
    for row in built["settings"]:
        if row["kind"] == "choice":
            assert row.get("choices"), row["key"]


def test_integer_settings_are_marked(built):
    by_key = {row["key"]: row for row in built["settings"]}
    # Целочисленность выводится из типа дефолта — тем же правилом, каким
    # config._validate приводит значение. Разъедься они, панель отдала бы
    # дробное число туда, где прогон ждёт целое.
    assert by_key["budget_cooldown_days"].get("integer") is True
    assert "integer" not in by_key["explore_share"]


def test_every_lane_is_described(built):
    assert [row["lane"] for row in built["lanes"]] == list(lanes.ALL_LANES)
    assert all(row["about"] for row in built["lanes"])


def test_lane_measure_days_come_from_the_lane_module(built):
    for row in built["lanes"]:
        assert row["measure_days"] == lanes.MEASURE_DAYS[row["lane"]]


def test_launch_lane_is_marked_manual(built):
    launch = next(r for r in built["lanes"] if r["lane"] == lanes.LANE_LAUNCH)
    assert launch["manual_release"] is True
    assert launch["default_step"] == 0


def test_every_step_of_the_ladder_is_described(built):
    assert [row["step"] for row in built["steps"]] == [s.step for s in autonomy.STEPS]
    assert [row["share"] for row in built["steps"]] == [s.share for s in autonomy.STEPS]


def test_every_tier_is_described(built):
    assert [row["tier"] for row in built["tiers"]] == list(tier.ALL_TIERS)
    assert all(row["title"] and row["about"] for row in built["tiers"])


def test_every_allowed_kind_is_on_the_map(built):
    # Вид с рычагом, но без полосы, на экране не появится вовсе: список
    # строится по карте полос. Такой вид применяется молча — и это ровно та
    # слепота, ради которой манифест и заведён.
    on_map = {row["kind"] for row in built["action_kinds"]}
    assert guardrails.ALLOWED_ACTION_KINDS <= on_map


def test_builder_kind_is_shown_with_its_reason(built):
    create = next(r for r in built["action_kinds"] if r["kind"] == "campaign.create")
    assert create["applied"] is False
    assert create["builder"] is True
    assert create["reason"] == guardrails.BUILDER_REASON


def test_added_modifier_is_rollable_into_neutral(built):
    # Плоское «откатывается: нет» соврало бы: bidmodifier.add отменяется не
    # собой, а перезаписью в нейтраль.
    add = next(r for r in built["action_kinds"] if r["kind"] == "bidmodifier.add")
    assert add["rollback"] is True and add["rollback_to"] == "neutral"
    suspend = next(r for r in built["action_kinds"] if r["kind"] == "campaign.suspend")
    assert suspend["rollback_to"] == "previous"


def test_pipeline_nodes_point_at_real_modules(built):
    for node in built["pipeline"]:
        path = os.path.join(REPO_ROOT, node["module"].replace("/", os.sep))
        assert os.path.exists(path), f"{node['id']}: нет {node['module']}"


def test_pipeline_edges_lead_somewhere(built):
    known = {node["id"] for node in built["pipeline"]}
    for node in built["pipeline"]:
        for nxt in node.get("next", []):
            assert nxt in known, f"{node['id']} → {nxt}"


def test_gates_carry_the_thresholds_the_run_uses(built):
    from sync.agent import gate

    assert built["gates"]["window_days"] == gate.GATE_WINDOW_DAYS
    assert built["gates"]["sum_settle_days"] == gate.SUM_SETTLE_DAYS
    assert built["gates"]["facts_max_age_days"] == gate.FACTS_MAX_AGE_DAYS


def test_presets_only_touch_known_keys(built):
    for name, values in built["presets"].items():
        assert set(values) <= set(config.SPEC), name
