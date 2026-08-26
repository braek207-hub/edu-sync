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


# ------------------- конверсии кандидата доезжают через витрину


def test_computed_rows_carry_conversions_of_the_cut_traffic():
    """Уберите этот тест — и конверсии вырезаемого трафика снова негде везти.

    До 27.08.2026 строка витрины дублировала клики в raw_value и support_n, а
    candidates_from_computed подставляла conversions=0 литералом. Следствий
    два, и оба бьют по деньгам: кандидат cpa_above_limit (конверсии есть, но
    дорогие) выглядел как zero_conversions, то есть получал класс 0 —
    «арифметика, риском не платит, вносится весь и сразу», — и обещал «лидов
    не теряем» ровно там, где режется конверсионный трафик.
    """
    from sync.agent.writer.negatives import (NEGATIVE_CONVERSIONS_KIND,
                                             computed_rows)
    rows = computed_rows([{
        "query": "мгсу цена", "cost": 30_000.0, "clicks": 300,
        "conversions": 5, "reason": "cpa_above_limit",
        "campaigns": ["1", "2"],
        "cost_by_campaign": {"1": 20_000.0, "2": 10_000.0},
        "conversions_by_campaign": {"1": 4, "2": 1},
    }])
    by_kind = {r["setting_kind"]: r for r in rows["1"]}
    assert by_kind[NEGATIVE_CONVERSIONS_KIND]["setting_key"] == "мгсу цена"
    assert by_kind[NEGATIVE_CONVERSIONS_KIND]["value"] == 4.0
    assert {r["setting_kind"] for r in rows["2"]} == {"negative_phrase",
                                                     NEGATIVE_CONVERSIONS_KIND}


def test_zero_conversions_are_written_too_so_that_silence_means_unknown():
    # Строка с нулём обязана существовать: только тогда ОТСУТСТВИЕ строки
    # честно означает «не измеряли», а не «конверсий не было».
    from sync.agent.writer.negatives import (NEGATIVE_CONVERSIONS_KIND,
                                             computed_rows)
    rows = computed_rows([_candidate("мгсу")])
    values = {r["setting_kind"]: r["value"] for r in rows["1"]}
    assert values[NEGATIVE_CONVERSIONS_KIND] == 0.0


def test_candidate_conversions_are_summed_back_from_computed():
    from sync.agent.writer.negatives import (NEGATIVE_CONVERSIONS_KIND,
                                             candidates_from_computed)
    computed = {
        "1": [{"setting_kind": "negative_phrase", "setting_key": "мгсу цена",
               "value": 20_000.0, "raw_value": 200, "support_n": 200},
              {"setting_kind": NEGATIVE_CONVERSIONS_KIND,
               "setting_key": "мгсу цена", "value": 4.0}],
        "2": [{"setting_kind": "negative_phrase", "setting_key": "мгсу цена",
               "value": 10_000.0, "raw_value": 100, "support_n": 100},
              {"setting_kind": NEGATIVE_CONVERSIONS_KIND,
               "setting_key": "мгсу цена", "value": 1.0}],
    }
    candidate = candidates_from_computed(computed)[0]
    assert candidate["conversions"] == 5.0
    assert candidate["conversions_by_campaign"] == {"1": 4.0, "2": 1.0}


def test_old_row_means_conversions_unknown_not_zero():
    """Строка старого формата не даёт скидку правила трёх.

    «Неизвестно» и «ноль» — разные вещи: на нуле действие становится классом 0
    и перестаёт платить риском. Строка, записанная до 27.08.2026, конверсий не
    несёт вовсе, и выдать её за ноль значило бы раздать бесплатные отсечения
    всему, что успела накопить витрина.
    """
    from sync.agent.writer.negatives import candidates_from_computed
    computed = {"1": [{"setting_kind": "negative_phrase", "setting_key": "мгсу",
                       "value": 46_902.0, "raw_value": 300, "support_n": 300}]}
    candidate = candidates_from_computed(computed)[0]
    assert candidate["conversions"] is None


def test_plan_uses_measured_conversions_per_campaign():
    # Разложение по деньгам было единственным вариантом, пока разрез
    # «фраза × кампания» конверсий не нёс. Теперь он их несёт, и приписывать
    # кампании долю по расходу вместо её собственных лидов больше незачем.
    plan = plan_negatives([{
        "query": "мгсу цена", "cost": 30_000.0, "clicks": 300,
        "conversions": 5.0, "reason": "cpa_above_limit",
        "campaigns": ["1", "2"],
        "cost_by_campaign": {"1": 20_000.0, "2": 10_000.0},
        "conversions_by_campaign": {"1": 1.0, "2": 4.0},
    }])
    assert plan["cut_conversions"] == {"1": 1.0, "2": 4.0}
    assert plan["unknown_conversions"] == []


def test_plan_marks_campaigns_whose_conversions_were_never_measured():
    """Уберите этот тест — и «не измеряли» снова станет «ноль».

    Кандидат, собранный из строки витрины старого формата, конверсий не несёт.
    Молчаливый ноль здесь означал бы «этот трафик не приносил лидов» — то есть
    класс 0 и отсечение без риск-бюджета по данным, которых никто не видел.
    """
    plan = plan_negatives([{
        "query": "мгсу", "cost": 30_000.0, "clicks": 300,
        "conversions": None, "reason": "zero_conversions",
        "campaigns": ["1"], "cost_by_campaign": {"1": 30_000.0},
    }])
    assert plan["desired"] == {"1": ["мгсу"]}
    assert "1" not in plan["cut_conversions"]
    assert plan["unknown_conversions"] == ["1"]


def test_one_unknown_phrase_poisons_only_its_own_campaign():
    # Кампания, у которой ВСЕ вырезаемые фразы измерены, скидку правила трёх
    # не теряет из-за соседней кампании с легаси-строкой.
    plan = plan_negatives([
        {"query": "мгсу", "cost": 30_000.0, "clicks": 300, "conversions": None,
         "reason": "zero_conversions", "campaigns": ["1"],
         "cost_by_campaign": {"1": 30_000.0}},
        {"query": "мгсу цена", "cost": 20_000.0, "clicks": 200,
         "conversions": 0.0, "reason": "zero_conversions", "campaigns": ["2"],
         "cost_by_campaign": {"2": 20_000.0},
         "conversions_by_campaign": {"2": 0.0}},
    ])
    assert plan["unknown_conversions"] == ["1"]
    assert plan["cut_conversions"] == {"2": 0.0}


# ------------------- основание класса 0 производится, а не подставляется тестом


def test_cut_carries_the_evidence_its_class_is_judged_by():
    """Уберите этот тест — и класс 0 умрёт на боевом пути, оставшись зелёным.

    writer/tier.tier_of считает отсечение арифметикой (риском не платит,
    вносится всё и сразу) только по полю evidence арифметической формы. До
    27.08.2026 evidence не производил НИКТО: ни этот рычаг, ни площадки. Тесты
    классов при этом были зелёные, потому что подставляли evidence сами, — а в
    бою каждая минус-фраза оказывалась классом 2 и главное обещание плана
    («класс 0 вносится весь и сразу») не работало ни разу.
    """
    from sync.agent.writer import tier
    plan = plan_negatives([{
        "query": "мгсу", "cost": 90_000.0, "clicks": 300, "conversions": 0.0,
        "reason": "zero_conversions", "campaigns": ["1"],
        "cost_by_campaign": {"1": 90_000.0},
        "conversions_by_campaign": {"1": 0.0},
    }])
    actions, _ = diff_negatives(
        plan["desired"], {"1": {"negative_keywords": [],
                                "campaign_type": "TEXT_CAMPAIGN"}},
        cut_cost=plan["cut_cost"], cut_conversions=plan["cut_conversions"],
        baseline_cpa=2_400.0)
    evidence = actions[0]["evidence"]
    assert evidence["cost_rub"] == 90_000.0
    assert evidence["conversions"] == 0.0
    assert evidence["baseline_cpa"] == 2_400.0
    assert evidence["window_days"] >= tier.MATURE_WINDOW_DAYS
    assert tier.tier_of(actions[0]) == tier.TIER_ARITHMETIC


def test_cut_of_converting_traffic_is_not_arithmetic():
    # Конверсии на вырезаемом трафике переводят отсечение в прогноз «дороже,
    # чем нам надо». Право резать без риск-бюджета такое действие не получает.
    from sync.agent.writer import tier
    plan = plan_negatives([{
        "query": "мгсу цена", "cost": 90_000.0, "clicks": 300,
        "conversions": 6.0, "reason": "cpa_above_limit", "campaigns": ["1"],
        "cost_by_campaign": {"1": 90_000.0},
        "conversions_by_campaign": {"1": 6.0},
    }])
    actions, _ = diff_negatives(
        plan["desired"], {"1": {"negative_keywords": [],
                                "campaign_type": "TEXT_CAMPAIGN"}},
        cut_cost=plan["cut_cost"], cut_conversions=plan["cut_conversions"],
        baseline_cpa=2_400.0)
    assert actions[0]["evidence"]["conversions"] == 6.0
    assert tier.tier_of(actions[0]) != tier.TIER_ARITHMETIC


def test_unmeasured_conversions_leave_the_evidence_incomplete():
    """Неизвестное не даёт скидку правила трёх.

    Кампания, чьи конверсии не измерены (строка витрины старого формата),
    основания «нуль конверсий» не имеет вовсе — и класс 0 не получает.
    """
    from sync.agent.writer import tier
    plan = plan_negatives([{
        "query": "мгсу", "cost": 90_000.0, "clicks": 300, "conversions": None,
        "reason": "zero_conversions", "campaigns": ["1"],
        "cost_by_campaign": {"1": 90_000.0},
    }])
    actions, _ = diff_negatives(
        plan["desired"], {"1": {"negative_keywords": [],
                                "campaign_type": "TEXT_CAMPAIGN"}},
        cut_cost=plan["cut_cost"], cut_conversions=plan["cut_conversions"],
        baseline_cpa=2_400.0)
    assert actions[0]["evidence"].get("conversions") is None
    assert tier.tier_of(actions[0]) != tier.TIER_ARITHMETIC


def test_evidence_is_absent_when_the_baseline_cpa_is_unknown():
    # Порога, по которому кандидат и выбран, нет — значит нечем показать, что
    # расход превысил три цены конверсии. Основание не выдумывается.
    plan = plan_negatives([{
        "query": "мгсу", "cost": 90_000.0, "clicks": 300, "conversions": 0.0,
        "reason": "zero_conversions", "campaigns": ["1"],
        "cost_by_campaign": {"1": 90_000.0},
        "conversions_by_campaign": {"1": 0.0},
    }])
    actions, _ = diff_negatives(
        plan["desired"], {"1": {"negative_keywords": [],
                                "campaign_type": "TEXT_CAMPAIGN"}},
        cut_cost=plan["cut_cost"], cut_conversions=plan["cut_conversions"])
    assert actions[0].get("evidence") is None
