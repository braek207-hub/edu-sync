# -*- coding: utf-8 -*-
"""
tests/test_agent_config_cli.py — ручки панели настроек (sync/agent_config.py).

Панель настроек умела только читаться: строки edu_agent_config накладывались
на кодовые дефолты, а записать в таблицу было нечем. Ручки, которые появились,
обязаны держать три свойства, и каждое здесь проверяется отдельно:

  * настройку либо приняли целиком, либо не приняли вовсе — половина набора
    оставляет агента в состоянии, которого человек не задавал;
  * отказ вместо умолчания: опечатка, значение вне диапазона и попытка тронуть
    порог защиты дают ненулевой код возврата и НИ ОДНОЙ записи;
  * записанное читается обратно ровно тем значением, которое задали, — и
    читает его не CLI, а sync/agent/db.py::load_agent_config, то есть проверка
    идёт у получателя.

БД подменяется целиком (fake-хранилище + подмена доступа в sync/agent/db.py),
поэтому тесты не требуют DATABASE_URL.
"""

from contextlib import contextmanager

import pytest

import sync.agent.db as agent_db
from sync.agent import blackbox
from sync.agent import config as agent_config
from sync.agent_config import (
    PRESET_KEY,
    Refusal,
    UsageError,
    main,
    parse_args,
    to_text,
    validate_pairs,
)


class FakeStore:
    """edu_agent_config в памяти: то же поведение upsert/delete, без БД."""

    def __init__(self):
        self.table = {}
        self.applies = 0

    def ensure(self):
        pass

    def rows(self):
        return [{"key": key, **row} for key, row in sorted(self.table.items())]

    def apply(self, upserts, deletes, actor):
        self.applies += 1
        for key, value in upserts.items():
            self.table[key] = {
                "value": value,
                "preset": upserts.get(PRESET_KEY) if key == PRESET_KEY else None,
                "updated_at": "2026-08-25T00:00:00+00:00",
                "updated_by": actor,
            }
        deleted = sum(1 for key in deletes if self.table.pop(key, None) is not None)
        return {"written": len(upserts), "deleted": deleted}


class _FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *args, **kwargs):
        return None


class _FakeConnection:
    def cursor(self, *args, **kwargs):
        return _FakeCursor()

    def commit(self):
        return None


@contextmanager
def _fake_connection():
    yield _FakeConnection()


@pytest.fixture
def store(monkeypatch):
    """Хранилище + чтение load_agent_config поверх него же.

    Читать после записи ОБЯЗАН настоящий load_agent_config: round-trip,
    проверенный собственным разборщиком CLI, ничего не доказывает — расходятся
    как раз два конца.
    """
    fake = FakeStore()
    monkeypatch.setattr(agent_db, "get_connection", _fake_connection)
    monkeypatch.setattr(
        agent_db, "_fetch_dicts",
        lambda sql, params=(): [{"key": r["key"], "value": r["value"],
                                 "preset": r["preset"]} for r in fake.rows()])
    monkeypatch.setattr(blackbox, "save_run", lambda *a, **k: {"saved": True})
    return fake


def _active(store_obj):
    stored = agent_db.load_agent_config()
    return agent_config.resolve(stored["preset"], stored["overrides"])


# ------------------------------------------------------------- разбор argv

def test_no_arguments_means_show():
    assert parse_args([])["action"] == "show"
    assert parse_args(["--show"])["action"] == "show"


def test_set_collects_every_pair_after_the_flag():
    intent = parse_args(["--set", "explore_share=0.12", "p_sign_bid=0.85"])
    assert intent["action"] == "set"
    assert intent["pairs"] == ["explore_share=0.12", "p_sign_bid=0.85"]


def test_preset_and_unset_are_parsed():
    assert parse_args(["--preset", "conservative"])["preset"] == "conservative"
    assert parse_args(["--preset=aggressive"])["preset"] == "aggressive"
    assert parse_args(["--unset", "explore_share", "preset"])["keys"] == [
        "explore_share", "preset"]


def test_two_actions_in_one_call_are_refused():
    # Пресет ставит семь параметров разом; «поставил пресет и тут же
    # переопределил два» — две разные правки, и в журнале они обязаны быть
    # видны раздельно.
    with pytest.raises(UsageError, match="одно действие"):
        parse_args(["--preset", "balanced", "--set", "explore_share=0.1"])


def test_unknown_argument_and_empty_action_are_refused():
    with pytest.raises(UsageError, match="неизвестный аргумент"):
        parse_args(["--yolo"])
    with pytest.raises(UsageError, match="хотя бы одну пару"):
        parse_args(["--set"])
    with pytest.raises(UsageError, match="хотя бы один ключ"):
        parse_args(["--unset"])


# ------------------------------------------------------------------ отказы

def test_unknown_key_is_refused_and_nothing_is_written(store):
    code = main(["--set", "explor_share=0.1"], store=store)

    assert code != 0
    assert store.applies == 0
    assert store.table == {}


def test_value_outside_range_is_refused(store):
    assert main(["--set", "explore_share=0.9"], store=store) != 0
    assert store.table == {}


def test_locked_key_refusal_names_the_real_reason(store, capsys):
    # «Неизвестный параметр» на пороге защиты — вранье: ключ известен, его
    # просто нельзя ослаблять настройками.
    code = main(["--set", "red_line_tolerance=5.0"], store=store)
    err = capsys.readouterr().err

    assert code != 0
    assert "правкой кода" in err
    assert "неизвестный параметр" not in err
    assert store.table == {}


def test_locked_key_cannot_be_unset_either(store):
    assert main(["--unset", "min_leads_for_verdict"], store=store) != 0
    assert store.applies == 0


def test_broken_second_pair_leaves_the_first_unwritten(store):
    # Главный инвариант записи: половина настройки — состояние, которого
    # человек не задавал и о котором не узнает.
    code = main(["--set", "explore_share=0.12", "p_sign_bid=99"], store=store)

    assert code != 0
    assert store.applies == 0
    assert store.table == {}
    assert _active(store)["explore_share"] == agent_config.DEFAULTS["explore_share"]


def test_preset_key_cannot_be_set_as_a_parameter(store):
    code = main(["--set", "preset=aggressive"], store=store)

    assert code != 0
    assert "preset" not in store.table


def test_unknown_preset_is_refused(store):
    assert main(["--preset", "ultra"], store=store) != 0
    assert store.table == {}


def test_empty_value_for_non_nullable_key_is_refused(store):
    # Пусто законно только там, где «не задано» — осмысленный ответ.
    assert main(["--set", "explore_share="], store=store) != 0
    assert store.table == {}


# ------------------------------------------------------- запись и round-trip

def test_written_values_come_back_exactly_as_given(store):
    code = main(["--set", "explore_share=0.12", "budget_cooldown_days=21",
                 "autonomy=suggest_only"], store=store)

    assert code == 0
    active = _active(store)
    assert active["explore_share"] == 0.12
    assert active["budget_cooldown_days"] == 21
    assert isinstance(active["budget_cooldown_days"], int)
    assert active["autonomy"] == "suggest_only"


def test_nullable_cap_can_be_set_and_cleared(store):
    # Тот самый разрыв между записью и чтением: пустое значение приезжало
    # обратно как '' и роняло валидацию, то есть снять потолок было нельзя.
    assert main(["--set", "monthly_budget_cap_rub=3000000"], store=store) == 0
    assert _active(store)["monthly_budget_cap_rub"] == 3_000_000.0

    assert main(["--set", "monthly_budget_cap_rub="], store=store) == 0
    assert store.table["monthly_budget_cap_rub"]["value"] == ""
    assert _active(store)["monthly_budget_cap_rub"] is None


def test_preset_is_stored_where_the_reader_looks_for_it(store):
    assert main(["--preset", "conservative"], store=store) == 0

    stored = agent_db.load_agent_config()
    assert stored["preset"] == "conservative"
    assert _active(store)["max_write_step"] == \
        agent_config.PRESETS["conservative"]["max_write_step"]


def test_unset_returns_the_parameter_to_the_preset_value(store):
    main(["--preset", "aggressive"], store=store)
    main(["--set", "explore_share=0.05"], store=store)
    assert _active(store)["explore_share"] == 0.05

    assert main(["--unset", "explore_share"], store=store) == 0
    assert _active(store)["explore_share"] == \
        agent_config.PRESETS["aggressive"]["explore_share"]


def test_an_orphaned_key_can_be_unset(store):
    # Ключ, ушедший из SPEC (переименование ручки), роняет все прогоны на
    # «неизвестном параметре» — --unset обязан уметь снять сироту, иначе её
    # нельзя вылечить вовсе. Кейс реален: consolidate_min_expected_payments →
    # consolidate_min_verdict_conversions (01.09.2026).
    store.table["retired_knob"] = {"key": "retired_knob", "value": "10",
                                   "preset": None, "updated_by": "old"}

    assert main(["--unset", "retired_knob"], store=store) == 0
    assert "retired_knob" not in store.table


def test_unset_preset_returns_to_code_defaults(store):
    main(["--preset", "aggressive"], store=store)
    assert main(["--unset", PRESET_KEY], store=store) == 0

    assert agent_db.load_agent_config()["preset"] is None
    assert _active(store)["max_write_step"] == agent_config.DEFAULTS["max_write_step"]


def test_author_of_the_change_is_recorded(monkeypatch, store):
    monkeypatch.setenv("AGENT_CONFIG_ACTOR", "pavel")
    main(["--set", "p_sign_bid=0.85"], store=store)
    assert store.table["p_sign_bid"]["updated_by"] == "pavel"

    monkeypatch.delenv("AGENT_CONFIG_ACTOR")
    main(["--set", "p_sign_bid=0.86"], store=store)
    assert store.table["p_sign_bid"]["updated_by"] == "cli"


def test_change_leaves_a_trail_in_the_blackbox(monkeypatch, store):
    # След обязателен: «почему агент с понедельника резче» без записи о смене
    # настройки превращается в археологию по коммитам.
    saved = []
    monkeypatch.setattr(blackbox, "save_run",
                        lambda run_id, stage, mode, report, **k:
                        saved.append({"stage": stage, "report": report})
                        or {"saved": True})

    main(["--set", "explore_share=0.12"], store=store)

    assert saved and saved[0]["stage"] == "config"
    assert saved[0]["report"]["change"]["set"] == {"explore_share": 0.12}


def test_failed_trail_never_fails_the_applied_change(monkeypatch, store):
    # Настройка уже применена: падение на записи следа оставило бы человека в
    # уверенности, что правка не прошла.
    monkeypatch.setattr(blackbox, "save_run",
                        lambda *a, **k: {"saved": False, "error": "нет базы"})

    assert main(["--set", "explore_share=0.12"], store=store) == 0
    assert _active(store)["explore_share"] == 0.12


def test_show_prints_every_parameter_with_its_source(store, capsys):
    main(["--preset", "conservative"], store=store)
    main(["--set", "explore_share=0.12"], store=store)
    capsys.readouterr()

    assert main([], store=store) == 0
    out = capsys.readouterr().out

    assert "conservative" in out
    for key in agent_config.SPEC:
        assert key in out
    assert "override" in out and "preset" in out
    assert "edu_agent_config" in out


def test_to_text_and_validation_agree_on_types():
    assert to_text(None) == ""
    assert to_text(21) == "21"
    assert validate_pairs(["budget_cooldown_days=21"]) == [("budget_cooldown_days", 21)]
    with pytest.raises(Refusal, match="KEY=VALUE"):
        validate_pairs(["explore_share"])


def test_lane_keys_accept_text_forms_from_the_workflow():
    # Воркфлоу agent-config передаёт args одной строкой: shadow_lanes и lane_steps
    # приезжают текстом, а валидаторы ждут list/dict. Без разбора текста оба ключа
    # из панели задать нельзя (run 33296975452: «нужен список полос»).
    assert validate_pairs(["shadow_lanes=suspend, allocation"]) == [
        ("shadow_lanes", ["allocation", "suspend"])]
    assert validate_pairs(['shadow_lanes=["suspend"]']) == [("shadow_lanes", ["suspend"])]
    assert validate_pairs(["lane_steps=tuning:3,hygiene:1"]) == [
        ("lane_steps", {"tuning": 3, "hygiene": 1})]
    assert validate_pairs(['lane_steps={"tuning": 3}']) == [("lane_steps", {"tuning": 3})]
    assert validate_pairs(["shadow_lanes="]) == [("shadow_lanes", None)]
    with pytest.raises(Refusal, match="tuning:3"):
        validate_pairs(["lane_steps=tuning=3"])
    with pytest.raises(Refusal, match="tuning:3"):
        validate_pairs(["lane_steps=tuning:"])
    with pytest.raises(Refusal, match="JSON"):
        validate_pairs(["shadow_lanes=[suspend"])


def test_composite_values_round_trip_through_the_text_column():
    # to_text писал str(dict) — Python-repr с одинарными кавычками, который
    # чтение (_parse_config_value, JSON) не разбирает: записанное не читалось.
    steps = {"tuning": 3, "hygiene": 1}
    assert agent_db._parse_config_value(to_text(steps)) == steps
    lanes = ["allocation", "suspend"]
    assert agent_db._parse_config_value(to_text(lanes)) == lanes

