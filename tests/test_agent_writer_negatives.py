# -*- coding: utf-8 -*-
"""Э3.6 (запись): минус-фразы кампании.

Рычаг накопительный: список минус-фраз в API заменяется ЦЕЛИКОМ, поэтому
действие всегда несёт объединение прежнего списка и добавляемых фраз, а
previous_state — прежний список для отката.

Отдельная опасность этого рычага в том, что он отсекает трафик НАВСЕГДА и
сразу: ошибка тут не «ставка выше», а «показов нет». Поэтому фраз за такт
немного, длина и слова проверяются до отправки, а суммарный лимит кабинета
(20 000 символов) считается независимо от построителя.
"""

import pytest

from sync.agent.writer.negatives import (
    MAX_PHRASES_PER_TICK,
    MAX_TOTAL_CHARS,
    MAX_WORDS_PER_PHRASE,
    NEGATIVE_KIND,
    diff_negatives,
    merge_phrases,
    normalize_phrase,
    plan_negatives,
    phrase_is_valid,
)


def _candidate(query, cost=20_000.0, campaigns=("1",), reason="zero_conversions"):
    return {"query": query, "cost": cost, "clicks": 300, "conversions": 0,
            "cpa": cost / 3, "reason": reason, "campaigns": list(campaigns)}


# ------------------------------------------------------------- нормализация


def test_phrase_is_lowercased_and_squeezed():
    assert normalize_phrase("  МГСУ   Официальный  Сайт ") == "мгсу официальный сайт"


def test_too_long_phrase_is_invalid():
    ok, reason = phrase_is_valid(" ".join(["слово"] * (MAX_WORDS_PER_PHRASE + 1)))
    assert not ok and "слов" in reason


def test_too_long_word_is_invalid():
    ok, reason = phrase_is_valid("а" * 36)
    assert not ok


def test_operators_are_refused():
    # Минус-фраза с операторами Директа («!», «+», кавычки) означает не то,
    # что кажется, и в автоматическом рычаге ей не место.
    for phrase in ('"мгсу"', "!мгсу", "+мгсу", "[мгсу]"):
        ok, _ = phrase_is_valid(phrase)
        assert not ok, phrase


# ------------------------------------------------------------------- план


def test_plan_groups_candidates_by_campaign():
    plan = plan_negatives([
        _candidate("мгсу", campaigns=("1", "2")),
        _candidate("мфюа", campaigns=("2",)),
    ])
    assert set(plan["desired"]) == {"1", "2"}
    assert plan["desired"]["1"] == ["мгсу"]
    assert plan["desired"]["2"] == ["мгсу", "мфюа"]


def test_plan_caps_phrases_per_tick_by_cost():
    plan = plan_negatives([_candidate(f"фраза {i}", cost=1000.0 * i)
                           for i in range(1, MAX_PHRASES_PER_TICK + 5)])
    assert len(plan["desired"]["1"]) == MAX_PHRASES_PER_TICK
    # Отсекается хвост по расходу: самые дорогие фразы остаются, дешёвые ждут
    # следующего такта (порядок внутри списка алфавитный — он идёт в ключ
    # идемпотентности и обязан быть детерминированным).
    assert f"фраза {MAX_PHRASES_PER_TICK + 4}" in plan["desired"]["1"]
    assert "фраза 1" not in plan["desired"]["1"]
    assert plan["over_cap"] == 4


def test_plan_drops_invalid_phrases_with_reason():
    plan = plan_negatives([_candidate("!мгсу"), _candidate("мгсу")])
    assert plan["desired"]["1"] == ["мгсу"]
    assert plan["invalid"][0]["query"] == "!мгсу"


# ---------------------------------------------------------------- слияние


def test_merge_keeps_existing_and_adds_new():
    merged = merge_phrases(["старая"], ["новая", "старая"])
    assert merged == ["новая", "старая"]


def test_merge_respects_the_total_char_budget():
    long_existing = [f"я{i:05d}" + "я" * 24 for i in range(MAX_TOTAL_CHARS // 30)]
    merged = merge_phrases(long_existing, ["я" * 30])
    assert sum(len(p) for p in merged) <= MAX_TOTAL_CHARS
    assert merged == sorted(long_existing)  # места нет — прежний список цел


# ---------------------------------------------------------------- разница


def test_action_carries_union_and_previous_state():
    actions, refused = diff_negatives({"1": ["мгсу"]},
                                      {"1": {"negative_keywords": ["мфюа"],
                                             "campaign_type": "TEXT_CAMPAIGN"}})
    assert not refused
    action = actions[0]
    assert action["action_kind"] == NEGATIVE_KIND
    assert action["payload"]["NegativeKeywords"]["Items"] == ["мгсу", "мфюа"]
    assert action["previous_state"]["NegativeKeywords"]["Items"] == ["мфюа"]
    assert action["idempotency_key"]


def test_no_action_when_phrases_already_there():
    actions, refused = diff_negatives({"1": ["мгсу"]},
                                      {"1": {"negative_keywords": ["мгсу"],
                                             "campaign_type": "TEXT_CAMPAIGN"}})
    assert not actions and not refused


def test_display_campaign_is_refused():
    actions, refused = diff_negatives({"1": ["мгсу"]},
                                      {"1": {"negative_keywords": [],
                                             "campaign_type": "MOBILE_APP_CAMPAIGN"}})
    assert not actions and refused


# ------------------- мост «расчёт → computed → писатель»


def test_computed_rows_carry_phrase_cost_and_reason():
    from sync.agent.writer.negatives import computed_rows
    rows = computed_rows([_candidate("мгсу", cost=46_902.0, campaigns=("1", "2"))])
    assert set(rows) == {"1", "2"}
    row = rows["1"][0]
    assert row["setting_kind"] == "negative_phrase"
    assert row["setting_key"] == "мгсу"
    assert row["value"] == 46_902.0
    assert row["raw_value"] == 300          # клики фразы
    assert row["reason"] == "zero_conversions"


def test_candidates_from_computed_restore_the_plan_input():
    from sync.agent.writer.negatives import candidates_from_computed
    computed = {"1": [{"setting_kind": "negative_phrase", "setting_key": "мгсу",
                       "value": 46_902.0, "raw_value": 300, "support_n": 300,
                       "reason": "zero_conversions"}],
                "2": [{"setting_kind": "negative_phrase", "setting_key": "мгсу",
                       "value": 10_000.0, "raw_value": 90, "support_n": 90,
                       "reason": "zero_conversions"},
                      {"setting_kind": "bid_modifier:device", "setting_key": "mobile",
                       "value": -20.0}]}
    candidates = candidates_from_computed(computed)
    assert len(candidates) == 1
    assert candidates[0]["query"] == "мгсу"
    assert candidates[0]["cost"] == 56_902.0        # суммарный расход фразы
    assert sorted(candidates[0]["campaigns"]) == ["1", "2"]
