# -*- coding: utf-8 -*-
"""
tests/test_agent_writer_tier.py — класс достоверности действия.

Данные — литералы в форме действия движка записи: модуль ничего не читает и
никуда не пишет, он отвечает на один вопрос — насколько мы уверены в этом
действии и надо ли за него платить риском.

Полоса и класс ортогональны, и тесты держат обе стороны: полоса отвечает «что
двигаем» (writer/lanes.py), класс — «платим ли риском». Свернуть их в одну
шкалу нельзя: вырезание гео живёт в полосе перераспределения, а классом
остаётся нулевым.
"""

import pathlib
import subprocess
import sys

import pytest

from sync.agent import objects
from sync.agent.writer import guardrails, lanes, tier


def _cut_evidence(cost_rub=9000.0, conversions=0, baseline_cpa=2400.0,
                  window_days=28):
    return {"cost_rub": cost_rub, "conversions": conversions,
            "baseline_cpa": baseline_cpa, "window_days": window_days}


# ------------------- четыре теста спеки (§Ф11, задача 5)


def test_zero_conversions_above_three_cpa_is_arithmetic():
    action = {"action_kind": "negative.add",
              "evidence": {"cost_rub": 9000.0, "conversions": 0,
                           "baseline_cpa": 2400.0, "window_days": 28}}
    assert tier.tier_of(action) == tier.TIER_ARITHMETIC


def test_immature_window_is_not_arithmetic():
    action = {"action_kind": "negative.add",
              "evidence": {"cost_rub": 9000.0, "conversions": 0,
                           "baseline_cpa": 2400.0, "window_days": 5}}
    assert tier.tier_of(action) > tier.TIER_ARITHMETIC


def test_action_without_evidence_is_never_arithmetic():
    assert tier.tier_of({"action_kind": "negative.add"}) > tier.TIER_ARITHMETIC


def test_bet_flag_forces_tier_two():
    action = {"action_kind": "negative.add", "payload": {"exploration": {"rub": 1}},
              "evidence": {"cost_rub": 9000.0, "conversions": 0,
                           "baseline_cpa": 2400.0, "window_days": 28}}
    assert tier.tier_of(action) == tier.TIER_BET


# ------------------- сама шкала


def test_four_tiers_are_ordered_zero_to_three():
    assert (tier.TIER_ARITHMETIC, tier.TIER_MEASURED,
            tier.TIER_BET, tier.TIER_PROPOSAL) == (0, 1, 2, 3)
    assert tier.ALL_TIERS == (0, 1, 2, 3)


def test_arithmetic_pays_no_risk_and_proposal_is_never_applied():
    # Две колонки таблицы §1.4 — данными, а не пересказом в комментарии:
    # иначе потребитель (задача 7) выведет их заново и разойдётся.
    assert tier.TIER_ARITHMETIC not in tier.RISK_PAYING_TIERS
    assert tier.TIER_MEASURED in tier.RISK_PAYING_TIERS
    assert tier.TIER_BET in tier.RISK_PAYING_TIERS

    assert tier.TIER_PROPOSAL not in tier.APPLIED_TIERS
    assert tier.TIER_PROPOSAL not in tier.RISK_PAYING_TIERS
    assert tier.APPLIED_TIERS == frozenset({0, 1, 2})


def test_every_allowed_kind_gets_a_known_tier():
    # Вид действия без класса прошёл бы отбор с неизвестной ценой уверенности.
    for kind in guardrails.ALLOWED_ACTION_KINDS:
        assert tier.tier_of({"action_kind": kind, "payload": {}}) in tier.ALL_TIERS, kind


# ------------------- класс 0: утверждение о прошлом


def test_placement_exclusion_is_arithmetic_on_the_same_rule():
    action = {"action_kind": "placement.exclude", "evidence": _cut_evidence()}
    assert tier.tier_of(action) == tier.TIER_ARITHMETIC


def test_spend_below_three_cpa_is_not_arithmetic():
    # 7 000 ₽ при цене 2 400 ₽ — ещё не три CPA: правило трёх такой ноль
    # объявить нулём не даёт.
    action = {"action_kind": "negative.add",
              "evidence": _cut_evidence(cost_rub=7000.0)}
    assert tier.tier_of(action) > tier.TIER_ARITHMETIC


def test_traffic_with_conversions_is_not_arithmetic():
    # Конверсии есть — режется конверсионный трафик, и это уже прогноз
    # «дороже, чем нам надо», а не утверждение о прошлом.
    action = {"action_kind": "negative.add",
              "evidence": _cut_evidence(cost_rub=90000.0, conversions=3)}
    assert tier.tier_of(action) > tier.TIER_ARITHMETIC


def test_evidence_without_baseline_cpa_is_not_arithmetic():
    action = {"action_kind": "negative.add",
              "evidence": {"cost_rub": 9000.0, "conversions": 0,
                           "window_days": 28}}
    assert tier.tier_of(action) > tier.TIER_ARITHMETIC


def _geo(new, old, evidence=True):
    """Действие географии литералами: только списки, без рычага."""
    action = {"action_kind": "geo.set",
              "payload": {"RegionIds": list(new)},
              "previous_state": {"RegionIds": list(old)}}
    if evidence:
        action["evidence"] = _cut_evidence()
    return action


def test_one_kind_gets_two_tiers_by_its_own_content():
    """Гео — единственный вид, у которого класс НЕ выводится из имени.

    Сужение — утверждение о прошлом (класс 0), расширение — ставка (класс 2),
    и решают это сами списки регионов, а не пометка построителя: иначе
    достаточно было бы назвать ход сужением, чтобы освободить его от риска.
    """
    assert tier.tier_of(_geo([213, 2], [213, 2, 65])) == tier.TIER_ARITHMETIC
    assert tier.tier_of(_geo([213, 2, 65], [213, 2])) == tier.TIER_BET


def test_a_geo_move_in_both_directions_shows_no_direction():
    # Убрали одно, добавили другое: ни сужение, ни расширение — значит ни
    # права резать без риска, ни переноса числа. Общий порядок, и класс 0
    # такому ходу не достаётся ни при каком основании.
    assert tier.tier_of(_geo([213, 43], [213, 65])) > tier.TIER_ARITHMETIC


def test_a_geo_move_without_a_known_past_is_not_arithmetic():
    # Прежний список неизвестен — сужение недоказуемо. «Не знаем» не имеет
    # права стать правом резать без риск-бюджета.
    assert tier.tier_of(_geo([213], [])) > tier.TIER_ARITHMETIC


def test_only_cutting_kinds_can_be_arithmetic():
    # То же основание на доливке бюджета — не арифметика: «ноль конверсий за
    # окно» ничего не утверждает о том, что будет после доливки.
    action = {"action_kind": "budget.set", "evidence": _cut_evidence()}
    assert tier.tier_of(action) > tier.TIER_ARITHMETIC


def test_returning_own_negative_is_not_arithmetic():
    # negative.remove_added живёт в полосе гигиены, но возвращает трафик, а не
    # снимает деньги с огня: класс по полосе выводить нельзя.
    action = {"action_kind": "negative.remove_added", "evidence": _cut_evidence()}
    assert tier.tier_of(action) > tier.TIER_ARITHMETIC


def test_thresholds_hold_exactly_at_their_edge():
    # Оба порога — на краю, иначе off-by-one живёт незамеченным: ровно три CPA
    # это ещё не «выше трёх CPA», а зрелость окна начинается С самого порога,
    # а не после него.
    def is_arithmetic(**kwargs):
        return tier.tier_of({"action_kind": "negative.add",
                             "evidence": _cut_evidence(**kwargs)})

    edge_cost = tier.CPA_MULTIPLE * 2400.0
    assert is_arithmetic(cost_rub=edge_cost) > tier.TIER_ARITHMETIC
    assert is_arithmetic(cost_rub=edge_cost + 1) == tier.TIER_ARITHMETIC

    assert is_arithmetic(window_days=tier.MATURE_WINDOW_DAYS - 1) > tier.TIER_ARITHMETIC
    assert is_arithmetic(window_days=tier.MATURE_WINDOW_DAYS) == tier.TIER_ARITHMETIC


def test_three_cpa_threshold_comes_from_objects_not_from_a_local_copy():
    # Вторая копия порога разъехалась бы с кандидатами при первой же правке
    # одной из них, и агент минусовал бы по одному правилу, а объяснял другим.
    assert tier.CPA_MULTIPLE == objects.ZERO_CONVERSION_RULE_OF_THREE


def test_mature_window_outlives_the_crm_lag_and_the_real_window_passes():
    # Лаг CRM 2–4 дня без дозревания (память edu-crm-lag-no-maturation):
    # окно обязано быть заметно длиннее лага, иначе «ноль конверсий» означает
    # «не приехало». И боевое окно кандидатов обязано его проходить, иначе
    # класс 0 мёртв по построению.
    assert tier.MATURE_WINDOW_DAYS >= 14
    assert objects.CANDIDATE_WINDOW_DAYS >= tier.MATURE_WINDOW_DAYS


# ------------------- класс 1 против класса 2


def test_lever_with_its_own_number_is_measured():
    action = {"action_kind": "bidmodifier.set",
              "payload": {"BidModifier": -30,
                          "expected_leads_delta": 0.79,
                          "expected_rub_delta": 0.0,
                          "expectation_basis": "сегмент 25.0% объекта × сдвиг -30%",
                          "expectation_days": 7}}
    assert tier.tier_of(action) == tier.TIER_MEASURED


def test_measurement_may_come_from_the_context_of_the_run():
    action = {"action_kind": "bidmodifier.set", "payload": {"BidModifier": -30}}
    context = {"segment_share": 0.25, "daily_cost_rub": 10000.0, "cpa_rub": 2400.0}
    assert tier.tier_of(action, context) == tier.TIER_MEASURED
    # Без контекста то же действие своего числа не имеет — значит не измерено.
    assert tier.tier_of(action) == tier.TIER_BET


def test_lever_without_a_measurement_is_a_bet():
    # Смена цели (Ф14) — основание есть, измерения нет: карман разведки.
    assert tier.tier_of({"action_kind": "goal.set", "payload": {}}) == tier.TIER_BET


# ------------------- класс 3: не применяется никогда


def test_proposal_kind_is_the_third_tier():
    action = {"action_kind": lanes.PROPOSAL_KIND_PREFIX + "landing",
              "payload": {}}
    assert tier.tier_of(action) == tier.TIER_PROPOSAL


def test_proposal_stays_third_even_with_an_exploration_flag():
    # Иначе смысловая гипотеза модели, помеченная разведкой, получила бы право
    # тратить деньги из разведочного кармана.
    action = {"action_kind": lanes.PROPOSAL_KIND_PREFIX + "offer",
              "payload": {"exploration": {"rub": 5000}}}
    assert tier.tier_of(action) == tier.TIER_PROPOSAL


def test_unknown_kind_has_no_lever_and_is_a_proposal():
    # Вида нет ни в одной полосе — рычага для него не существует. Класс 3
    # ничего не применит; громко падает на своём месте карта полос.
    assert tier.tier_of({"action_kind": "landing.rewrite"}) == tier.TIER_PROPOSAL
    with pytest.raises(ValueError):
        lanes.lane_of({"action_kind": "landing.rewrite"})


# ------------------- объявленный класс


def test_declared_tier_can_only_tighten():
    # Идея приносит свой класс (задачи 12–15). Он вправе ужесточить вывод —
    # гипотеза модели остаётся предложением, даже надев форму минус-фразы, —
    # но не вправе удешевить: объявить себе класс 0 значило бы выписать себе
    # освобождение от риска.
    proposal_shaped_as_a_cut = {"action_kind": "negative.add",
                                "tier": tier.TIER_PROPOSAL,
                                "evidence": _cut_evidence()}
    assert tier.tier_of(proposal_shaped_as_a_cut) == tier.TIER_PROPOSAL

    self_declared_arithmetic = {"action_kind": "budget.set",
                                "tier": tier.TIER_ARITHMETIC, "payload": {}}
    assert tier.tier_of(self_declared_arithmetic) == tier.TIER_BET


# ------------------- кольцо импортов


@pytest.mark.parametrize("module", [
    "sync.agent.writer.tier",
    "sync.agent.writer.lanes",
    "sync.agent.writer.expectation",
    "sync.agent.writer.switch",
])
def test_any_of_the_ring_can_be_imported_first(module):
    """Порядок импортов не должен решать, соберётся ли пакет.

    Кольцо lanes → switch → expectation → lanes уже есть и держится ленивым
    импортом; класс достоверности добавляет к нему tier → lanes, а его
    потребитель (lanes.select, задача 7) замкнёт lanes → tier. Проверка идёт
    отдельным интерпретатором: внутри сессии модули уже загружены, и любой
    порядок «работает» задним числом.
    """
    result = subprocess.run([sys.executable, "-c", f"import {module}"],
                            cwd=str(pathlib.Path(__file__).resolve().parents[1]),
                            capture_output=True)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
