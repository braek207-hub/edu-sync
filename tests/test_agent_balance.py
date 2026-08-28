# -*- coding: utf-8 -*-
"""Баланс такта: сокращение обязано иметь адресата, такт — не сжимать объём.

Механизм оптимизации, у которого единственный рычаг — резать неэффективное,
сходится к «дорого и мало»: каждая итерация улучшает среднее и уменьшает
объём. Требование продукта — рост И эффективность, поэтому у сокращения
обязан быть адресат, а такт целиком не имеет права уменьшать ожидаемые лиды.
"""

from sync.agent.balance import (
    EMERGENCY_KINDS,
    MIN_ASSIGNED_SHARE,
    balance_inputs,
    require_growth_address,
    tact_balance,
)
from sync.agent.writer.guardrails import ALLOWED_ACTION_KINDS


def _move(cid, cost, target, leads_delta):
    return {"campaign_id": cid, "cost_28d": cost, "target_28d": target,
            "expected_leads_delta": leads_delta}


# ----------------------------------------------------------------- баланс

def test_freed_money_is_matched_by_additions():
    moves = [_move("111", 100_000.0, 60_000.0, -8.0),
             _move("222", 100_000.0, 140_000.0, +12.0)]
    balance = tact_balance(moves, suspends=[], cuts=[])
    assert balance["freed_rub"] == 40_000.0
    assert balance["added_rub"] == 40_000.0
    assert balance["unassigned_rub"] == 0.0
    assert balance["shrinking"] is False


def test_suspend_without_reassignment_leaves_money_unassigned():
    # Выключение кампании освобождает её расход, и он обязан быть кому-то отдан.
    moves = [_move("222", 100_000.0, 100_000.0, 0.0)]
    balance = tact_balance(moves, suspends=[{"campaign_id": "111",
                                             "cost_28d": 50_000.0,
                                             "expected_leads_delta": -4.0}],
                           cuts=[])
    assert balance["freed_rub"] == 50_000.0
    assert balance["unassigned_rub"] == 50_000.0
    assert balance["shrinking"] is True


def test_negative_and_placement_cuts_count_as_shrink():
    # Потеря объёма НЕ измерена (leads_measured отсутствует): ноль в лидах
    # означает «не мерили», и рубли остаются единственной защитой от сжатия.
    balance = tact_balance([], suspends=[],
                           cuts=[{"kind": "negative.add", "cost_saved": 12_000.0,
                                  "expected_leads_delta": -1.0}])
    assert balance["freed_rub"] == 12_000.0
    assert balance["shrinking"] is True


def test_measured_cut_reallocates_instead_of_freeing():
    # Отсечение с ИЗМЕРЕННОЙ потерей объёма недельный лимит кампании не
    # трогает: те же деньги стратегия перекладывает на оставшиеся запросы.
    # Освобождением это не является, адресата не требует, и такт от него
    # сжимающим не становится.
    balance = tact_balance([], suspends=[],
                           cuts=[{"kind": "negative.add", "cost_saved": 12_000.0,
                                  "leads_measured": True,
                                  "expected_leads_delta": 0.0}])
    assert balance["freed_rub"] == 0.0
    assert balance["reallocated_rub"] == 12_000.0
    assert balance["shrinking"] is False


def test_measured_cut_that_loses_volume_still_shrinks():
    # Измерение показало, что вырезаемое давало конверсии, — это сжатие, и
    # плата берётся объёмом. Рубли при этом по-прежнему не «освобождаются»:
    # лимит кампании не изменился.
    balance = tact_balance([], suspends=[],
                           cuts=[{"kind": "negative.add", "cost_saved": 12_000.0,
                                  "leads_measured": True,
                                  "expected_leads_delta": -3.0}])
    assert balance["freed_rub"] == 0.0
    assert balance["reallocated_rub"] == 12_000.0
    assert balance["expected_leads_delta"] == -3.0
    assert balance["shrinking"] is True


def test_gate_keeps_measured_cuts_while_dropping_unmeasured_ones():
    # Живой случай 27–28.08.2026: такт сжимался доливкой без адресата, и
    # require_growth_address снимала самые дешёвые действия — то есть всю
    # гигиену. Измеренное отсечение объёма не отнимает и сниматься не должно.
    actions = [
        {"action_kind": "negative.add", "object_id": "111",
         "leads_measured": True, "expected_leads_delta": 0.0,
         "expected_gain_rub": 200.0},
        {"action_kind": "campaign.suspend", "object_id": "222",
         "expected_leads_delta": -4.0, "expected_gain_rub": 50_000.0},
    ]
    balance = {"shrinking": True, "expected_leads_delta": -4.0,
               "freed_rub": 50_000.0, "added_rub": 0.0, "freed_by_key": {}}
    allowed, blocked = require_growth_address(actions, balance)
    assert [a["object_id"] for a in blocked] == ["222"]
    assert [a["object_id"] for a in allowed] == ["111"]


def test_small_unassigned_tail_is_not_a_shrink():
    # Округления шага и капы оставляют хвост; придираться к нему значит
    # блокировать такт из-за копеек — порог MIN_ASSIGNED_SHARE именно об этом.
    moves = [_move("111", 100_000.0, 90_000.0, -1.0),
             _move("222", 100_000.0, 109_500.0, +2.0)]
    balance = tact_balance(moves, suspends=[], cuts=[])
    assert balance["assigned_share"] >= MIN_ASSIGNED_SHARE
    assert balance["shrinking"] is False


def test_emergency_entry_does_not_make_tact_shrinking():
    # Аварийное сокращение в баланс входит только справочно: иначе один откат
    # делал бы весь такт «сжимающим» и запирал бы обычные решения.
    balance = tact_balance(
        [], suspends=[{"campaign_id": "111", "cost_28d": 50_000.0,
                       "expected_leads_delta": -4.0, "emergency": True}],
        cuts=[])
    assert balance["freed_rub"] == 0.0
    assert balance["emergency_freed_rub"] == 50_000.0
    assert balance["expected_leads_delta"] == 0.0
    assert balance["shrinking"] is False


def test_balance_inputs_carry_measurement_flag_from_the_action():
    # Признак ставит тот, кто знает: writer/negatives.py и
    # writer/placements.py. Восстановить его здесь по нулю в лидах нельзя —
    # ноль двузначен, — поэтому он обязан доехать с самим действием.
    rows = balance_inputs(
        [{"action_kind": "negative.add", "object_id": "111",
          "leads_measured": True, "expected_leads_delta": 0.0},
         {"action_kind": "placement.exclude", "object_id": "222",
          "expected_leads_delta": 0.0}],
        moves_by_campaign={},
        cost_28d_by_campaign={},
        cut_cost_by_kind={"negative.add": {"111": 9_000.0},
                          "placement.exclude": {"222": 4_000.0}})
    measured = {c["campaign_id"]: c["leads_measured"] for c in rows["cuts"]}
    assert measured == {"111": True, "222": False}


def test_freed_money_is_addressable_per_action():
    # Гейт снимает действия по одному и обязан знать, сколько денег возвращает
    # каждое снятие, иначе он не может остановиться на первом достаточном.
    balance = tact_balance(
        [], suspends=[{"campaign_id": "111", "cost_28d": 50_000.0,
                       "idempotency_key": "k-suspend"}],
        cuts=[{"kind": "negative.add", "cost_saved": 12_000.0,
               "idempotency_key": "k-neg"}])
    assert balance["freed_by_key"] == {"k-suspend": 50_000.0, "k-neg": 12_000.0}


# ------------------------------------------------------------------ гейт

def test_growth_gate_drops_weakest_cut_until_tact_grows():
    # Сжимающий такт: два сокращения и одна доливка. Снимается то сокращение,
    # чей выигрыш меньше, — пока баланс не перестанет быть отрицательным.
    actions = [
        {"action_kind": "campaign.suspend", "object_id": "111",
         "expected_leads_delta": -4.0, "expected_gain_rub": 1_000.0},
        {"action_kind": "negative.add", "object_id": "333",
         "expected_leads_delta": -3.0, "expected_gain_rub": 200.0},
        {"action_kind": "budget.set", "object_id": "222",
         "expected_leads_delta": +5.0, "expected_gain_rub": 5_000.0},
    ]
    allowed, blocked = require_growth_address(
        actions, {"expected_leads_delta": -2.0, "shrinking": True})
    assert [a["object_id"] for a in blocked] == ["333"]
    assert len(allowed) == 2


def test_growth_gate_is_noop_when_tact_grows():
    actions = [{"action_kind": "budget.set", "object_id": "222",
                "expected_leads_delta": +8.0, "expected_gain_rub": 5_000.0}]
    allowed, blocked = require_growth_address(
        actions, {"expected_leads_delta": 8.0, "shrinking": False})
    assert allowed == actions and blocked == []


def test_growth_gate_drops_lone_cut_when_nothing_compensates():
    # Единственное действие такта — сокращение, компенсировать нечем. Оно
    # снимается: пустой такт честнее такта, который только сжимает.
    actions = [{"action_kind": "campaign.suspend", "object_id": "111",
                "expected_leads_delta": -9.0, "expected_gain_rub": 1_000.0}]
    allowed, blocked = require_growth_address(
        actions, {"expected_leads_delta": -9.0, "shrinking": True})
    assert allowed == []
    assert "адресат" in blocked[0]["blocked_reason"]


def test_growth_gate_reads_expectation_from_payload():
    # У боевого действия ожидание солвера лежит в payload (writer/budget.py:
    # _expectation_payload), а не верхним полем. Гейт, читающий только верхний
    # уровень, считал бы каждую доливку нулевой и резал бы её как сокращение.
    actions = [
        {"action_kind": "budget.set", "object_id": "222",
         "payload": {"expected_leads_delta": 5.0}},
        {"action_kind": "budget.set", "object_id": "111",
         "payload": {"expected_leads_delta": -3.0}},
    ]
    allowed, blocked = require_growth_address(
        actions, {"expected_leads_delta": 2.0, "shrinking": False})
    assert blocked == []
    assert len(allowed) == 2


def test_growth_gate_drops_cut_when_money_has_no_address():
    # Такт не уменьшает ожидаемые лиды (выключению их никто не посчитал), но
    # освобождённые деньги никому не назначены. Это тот же отказ от роста.
    actions = [{"action_kind": "campaign.suspend", "object_id": "111",
                "idempotency_key": "k-suspend"}]
    balance = {"shrinking": True, "expected_leads_delta": 0.0,
               "freed_rub": 50_000.0, "added_rub": 0.0,
               "freed_by_key": {"k-suspend": 50_000.0}}
    allowed, blocked = require_growth_address(actions, balance)
    assert allowed == []
    assert [a["object_id"] for a in blocked] == ["111"]


def test_growth_gate_stops_when_freed_money_is_covered():
    # Снятие первого же выключения возвращает деньги, и второе снимать незачем:
    # гейт не резак, а условие «у сокращения есть адресат».
    actions = [
        {"action_kind": "campaign.suspend", "object_id": "111",
         "idempotency_key": "k1"},
        {"action_kind": "negative.add", "object_id": "333",
         "idempotency_key": "k3"},
        {"action_kind": "budget.set", "object_id": "222",
         "payload": {"expected_leads_delta": 6.0}},
    ]
    balance = {"shrinking": True, "expected_leads_delta": 6.0,
               "freed_rub": 45_000.0, "added_rub": 40_000.0,
               "freed_by_key": {"k1": 44_000.0, "k3": 1_000.0}}
    allowed, blocked = require_growth_address(actions, balance)
    assert [a["object_id"] for a in blocked] == ["333"]
    assert len(allowed) == 2


def test_emergency_cut_passes_gate_even_when_tact_shrinks():
    # Откат по красной линии режет расход и адресата не имеет по определению.
    # Гейт, снявший откат, оставил бы в кабинете изменение, уже признанное
    # убыточным, — это дороже сжатия объёма.
    actions = [{"action_kind": "campaign.suspend", "object_id": "111",
                "emergency": True, "expected_leads_delta": -20.0}]
    kept, blocked = require_growth_address(
        actions, {"shrinking": True, "expected_leads_delta": -20.0,
                  "freed_rub": 50_000.0, "added_rub": 0.0})
    assert kept == actions
    assert blocked == []


def test_emergency_kinds_are_real_action_kinds():
    # Вид, которого нет в конвейере записи, в аварийном множестве быть не
    # должен: он выглядел бы работающим исключением, ничего не исключая.
    assert set(EMERGENCY_KINDS) <= set(ALLOWED_ACTION_KINDS)


# --------------------------------------------------- сборка входа из такта

def _budget_action(cid, key, leads_delta):
    return {"action_kind": "budget.set", "object_id": cid,
            "idempotency_key": key,
            "payload": {"expected_leads_delta": leads_delta}}


def test_balance_inputs_reads_budget_move_from_plan():
    actions = [_budget_action("111", "k1", -8.0), _budget_action("222", "k2", 12.0)]
    moves_by_campaign = {
        "111": {"cost_28d": 100_000.0, "target_28d": 60_000.0,
                "expected_leads_delta": -8.0},
        "222": {"cost_28d": 100_000.0, "target_28d": 140_000.0,
                "expected_leads_delta": 12.0},
    }
    parts = balance_inputs(actions, moves_by_campaign, {}, {})
    balance = tact_balance(**parts)
    assert balance["freed_rub"] == 40_000.0
    assert balance["added_rub"] == 40_000.0
    assert balance["shrinking"] is False


def test_balance_inputs_loses_no_money_when_growth_is_locked():
    # Гейты стоят ПОСЛЕ солвера: кампания, которой солвер назначил рост, могла
    # не дойти сюда (кулдаун обучения, потолок попыток). Её рубли обязаны
    # остаться неназначенными, а не исчезнуть вместе со строкой.
    actions = [_budget_action("111", "k1", -8.0)]      # доливка 222 заперта
    moves_by_campaign = {
        "111": {"cost_28d": 100_000.0, "target_28d": 60_000.0,
                "expected_leads_delta": -8.0},
        "222": {"cost_28d": 100_000.0, "target_28d": 140_000.0,
                "expected_leads_delta": 12.0},
    }
    balance = tact_balance(**balance_inputs(actions, moves_by_campaign, {}, {}))
    assert balance["added_rub"] == 0.0
    assert balance["unassigned_rub"] == 40_000.0
    assert balance["shrinking"] is True


def test_balance_inputs_prices_suspend_by_campaign_cost():
    actions = [{"action_kind": "campaign.suspend", "object_id": "111",
                "idempotency_key": "k-s"}]
    balance = tact_balance(**balance_inputs(
        actions, {}, {"111": 50_000.0}, {}))
    assert balance["freed_rub"] == 50_000.0
    assert balance["freed_by_key"] == {"k-s": 50_000.0}


def test_balance_inputs_prices_cuts_by_kind():
    # Минус-фразы и площадки считают вырезанный расход каждый своим словарём:
    # у одной кампании бывают оба рычага, и общий словарь потерял бы один.
    actions = [{"action_kind": "negative.add", "object_id": "111",
                "idempotency_key": "k-n"},
               {"action_kind": "placement.exclude", "object_id": "111",
                "idempotency_key": "k-p"}]
    cut_cost_by_kind = {"negative.add": {"111": 12_000.0},
                        "placement.exclude": {"111": 3_000.0}}
    balance = tact_balance(**balance_inputs(actions, {}, {}, cut_cost_by_kind))
    assert balance["freed_rub"] == 15_000.0


def test_balance_inputs_ignores_actions_without_money_meaning():
    # Корректировки и расписание расход не освобождают и не назначают: их
    # присутствие не должно делать такт «сжимающим».
    actions = [{"action_kind": "bidmodifier.set", "object_id": "111",
                "idempotency_key": "k-b"},
               {"action_kind": "schedule.set", "object_id": "111",
                "idempotency_key": "k-h"}]
    balance = tact_balance(**balance_inputs(actions, {}, {}, {}))
    assert balance["freed_rub"] == 0.0
    assert balance["added_rub"] == 0.0
    assert balance["shrinking"] is False


def test_tact_is_judged_by_the_calibrated_expectation_when_it_exists():
    """Сжимает ли такт объём — вопрос про то, что действительно случится.

    Модель, систематически завышающая эффект доливки, обещала бы рост там,
    где его нет: сырые числа дают +6 лидов и такт выглядит растущим, а
    история собственных промахов ужимает доливку до +1 — и тот же такт
    оказывается сжимающим. Поправка есть — судим по ней; истории нет — по
    сырому числу, как раньше.
    """
    actions = [
        {"action_kind": "budget.set", "object_id": "222",
         "payload": {"expected_leads_delta": 9.0,
                     "expected_leads_delta_calibrated": 1.0}},
        {"action_kind": "budget.set", "object_id": "111",
         "payload": {"expected_leads_delta": -3.0,
                     "expected_leads_delta_calibrated": -3.0}},
    ]
    moves_by_campaign = {"222": {"cost_28d": 100_000.0, "target_28d": 140_000.0},
                         "111": {"cost_28d": 100_000.0, "target_28d": 60_000.0}}
    parts = balance_inputs(actions, moves_by_campaign, {}, {})
    assert [m["expected_leads_delta"] for m in parts["moves"]] == [1.0, -3.0]

    balance = tact_balance(parts["moves"], parts["suspends"], parts["cuts"])
    assert balance["expected_leads_delta"] == -2.0
    assert balance["shrinking"] is True


def test_raw_expectation_is_used_while_there_is_no_history():
    # Пока пар «прогноз / факт» нет, поправки нет тоже, и гейт обязан читать
    # сырое число: иначе первые недели работы такт судился бы по нулю.
    actions = [{"action_kind": "budget.set", "object_id": "222",
                "payload": {"expected_leads_delta": 5.0}}]
    parts = balance_inputs(actions, {"222": {"cost_28d": 1.0, "target_28d": 2.0}},
                           {}, {})
    assert parts["moves"][0]["expected_leads_delta"] == 5.0
