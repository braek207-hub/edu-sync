# -*- coding: utf-8 -*-
import sync.agent.writer.risk as risk_module
from sync.agent.writer.risk import action_risk, fit_into_budget, median_daily_cost, week_start

# Единица риска — ОБЪЕКТ, на который действие влияет. В фикстурах ниже у
# каждого действия свой object_id: они изображают разные кампании, поэтому
# бюджет платит за каждое. Случай нескольких действий по ОДНОЙ кампании —
# отдельными тестами в конце файла.


def _act(key, object_id=None):
    return {"idempotency_key": key, "object_level": "campaign",
            "object_id": object_id or key}


def test_week_start_is_monday():
    assert week_start("2026-08-19") == "2026-08-17"  # среда → понедельник


def test_risk_is_spend_until_measurement():
    # Цена ошибки = дневной расход объекта × дни до обнаружения.
    action = {"object_id": "111"}
    risk = action_risk(action, {"111": 1000.0}, days_to_measure=7)
    assert risk == 7000.0


def test_risk_zero_when_campaign_known_with_zero_spend():
    # Кампания ЕСТЬ в справочнике со значением 0 — тратить нечего, риск честно 0.
    action = {"object_id": "111"}
    assert action_risk(action, {"111": 0.0, "222": 3000.0}) == 0.0


def test_risk_falls_back_to_median_when_campaign_unknown():
    # Кампании НЕТ в справочнике — расход неизвестен, а не нулевой. Молчаливый
    # ноль означает «бюджет не нужен», хотя на деле про кампанию ничего не
    # известно (лаг синка / новая кампания / пробел в источнике). Консервативная
    # оценка — медиана известных дневных расходов, не 0.
    daily_cost = {"111": 1000.0, "222": 3000.0}
    risk = action_risk({"object_id": "999"}, daily_cost, days_to_measure=7)
    assert risk == 14000.0  # median([1000, 3000]) = 2000 × 7
    assert risk != 0.0


def test_risk_is_infinite_when_no_cost_data_at_all():
    # Справочник пуст целиком — консервативную оценку взять неоткуда. Риск
    # неопределён и обязан не пройти бюджет молча нулём, а всегда уйти в
    # отложенные — поэтому +inf, а не 0.
    risk = action_risk({"object_id": "999"}, {})
    assert risk == float("inf")


def test_median_daily_cost_odd_count():
    assert median_daily_cost({"a": 100.0, "b": 300.0, "c": 200.0}) == 200.0


def test_median_daily_cost_even_count():
    assert median_daily_cost({"a": 1000.0, "b": 3000.0}) == 2000.0


def test_median_daily_cost_empty_is_none():
    assert median_daily_cost({}) is None


def test_fit_stops_at_budget():
    actions = [_act("a"), _act("b"), _act("c")]
    risks = {"a": 4000.0, "b": 4000.0, "c": 4000.0}
    fits, deferred = fit_into_budget(actions, risks, remaining_rub=9000.0)
    assert [a["idempotency_key"] for a in fits] == ["a", "b"]
    assert [a["idempotency_key"] for a in deferred] == ["c"]


def test_fit_takes_nothing_when_budget_exhausted():
    actions = [_act("a")]
    fits, deferred = fit_into_budget(actions, {"a": 100.0}, remaining_rub=0.0)
    assert fits == []
    assert len(deferred) == 1


def test_fit_allows_all_when_budget_large():
    actions = [_act("a"), _act("b")]
    fits, deferred = fit_into_budget(actions, {"a": 1.0, "b": 1.0}, remaining_rub=1_000_000.0)
    assert len(fits) == 2
    assert deferred == []


def test_fit_defers_action_with_undetermined_risk():
    # Действие с неопределённым риском (+inf) не проходит бюджет ни при каком
    # конечном остатке — уходит в отложенные, а не пропускается бесплатно.
    actions = [_act("unknown"), _act("known")]
    risks = {"unknown": float("inf"), "known": 100.0}
    fits, deferred = fit_into_budget(actions, risks, remaining_rub=1_000_000.0)
    assert [a["idempotency_key"] for a in fits] == ["known"]
    assert [a["idempotency_key"] for a in deferred] == ["unknown"]


# ------------------------------- риск списывается ПО ОБЪЕКТУ, а не по действию
# Дефект 4: оценка риска (дневной расход кампании × горизонт наблюдения)
# начислялась КАЖДОМУ действию. Четыре корректировки одной кампании списывали
# четырёхкратный расход — бюджет выгорал на второй-третьей кампании, и
# посчитанные корректировки капали по паре в неделю.


def test_risk_object_is_the_object_not_the_action():
    a = _act("k1", object_id="111")
    b = _act("k2", object_id="111")
    risk_object = risk_module.risk_object
    assert risk_object(a) == risk_object(b)
    assert risk_object(_act("k3", object_id="222")) != risk_object(a)


def test_each_change_pays_its_own_delta_up_to_the_object_cap():
    # Дельта-модель: каждая правка платит СВОЮ цену, а не «первая платит за
    # всю кампанию, остальные бесплатно». Сумма списаний по объекту ограничена
    # его потолком — больше, чем кампания тратит за горизонт, потерять на ней
    # нельзя, сколько бы правок в неё ни внесли.
    actions = [_act("a", object_id="111"), _act("b", object_id="111"),
               _act("c", object_id="111")]
    risks = {"a": 3000.0, "b": 3000.0, "c": 3000.0}
    caps = {"campaign:111": 7000.0}

    fits, deferred = fit_into_budget(actions, risks, remaining_rub=10_000.0,
                                     caps=caps)

    assert [a["idempotency_key"] for a in fits] == ["a", "b", "c"]
    assert deferred == []
    # Третья правка упирается в потолок объекта и доплачивает только остаток.
    assert [a["risk_rub"] for a in fits] == [3000.0, 3000.0, 1000.0]
    assert sum(a["risk_rub"] for a in fits) == 7000.0


def test_different_campaigns_are_charged_separately():
    actions = [_act("a", object_id="111"), _act("b", object_id="222")]
    risks = {"a": 7000.0, "b": 7000.0}

    fits, deferred = fit_into_budget(actions, risks, remaining_rub=10_000.0)

    assert [a["idempotency_key"] for a in fits] == ["a"]
    assert [a["idempotency_key"] for a in deferred] == ["b"]


def test_charged_amounts_carry_across_calls_within_one_run():
    # Счёт списаний по объекту общий на прогон: кабинеты обрабатываются по
    # очереди, и второй вызов обязан видеть, сколько по объекту уже списано.
    charged = {}
    caps = {"campaign:111": 7000.0}
    fit_into_budget([_act("a", object_id="111")], {"a": 5000.0}, 10_000.0,
                    charged, caps)

    fits, _ = fit_into_budget([_act("b", object_id="111")], {"b": 5000.0},
                              10_000.0, charged, caps)

    # Остаток до потолка объекта — 2000, его и платит вторая правка.
    assert fits[0]["risk_rub"] == 2000.0


def test_deferred_action_does_not_mark_object_as_paid():
    # Действие, не влезшее в бюджет, объект не помечает — иначе следующее
    # действие по той же кампании прошло бы бесплатно за счёт так и не
    # применённого первого.
    charged = {}
    actions = [_act("a", object_id="111"), _act("b", object_id="111")]
    risks = {"a": 7000.0, "b": 7000.0}

    fits, deferred = fit_into_budget(actions, risks, remaining_rub=100.0,
                                     charged_by_object=charged)

    assert fits == []
    assert len(deferred) == 2
    assert charged == {}


# --- недельный лимит: доля расхода, а не константа -----------------------

def test_weekly_limit_scales_with_account_spend():
    # 50 000 ₽ — это 0,9 % от недельного расхода кабинета в 5,7 млн ₽
    # (замер 26.08.2026). Константа не масштабируется: вырос кабинет вдвое —
    # агент остался таким же зажатым.
    assert risk_module.weekly_limit(5_700_000.0, 0.01, None) == 57_000.0
    assert risk_module.weekly_limit(11_400_000.0, 0.01, None) == 114_000.0


def test_absolute_override_wins_over_share():
    # Абсолютное значение ставит человек (risk_budget_week в LOCKED_KEYS) —
    # оно и решает, каким бы ни был расход.
    assert risk_module.weekly_limit(5_700_000.0, 0.01, 50_000.0) == 50_000.0


def test_unknown_spend_falls_back_to_default_not_to_zero():
    # Ноль означал бы «агент не работает» при первом же пробеле в витрине —
    # отказ, неотличимый от исправной остановки.
    assert risk_module.weekly_limit(0.0, 0.01, None) == risk_module.DEFAULT_WEEKLY_RISK_RUB


# --- цена ТАКТА: перенос не платит обеими сторонами -------------------------
# Дефект 8б плана беты: перенос 100 000 ₽ с кампании A на кампанию B стоил
# риском обе дельты — и −100 000 у A, и +100 000 у B. Под ударом здесь не
# 200 000 ₽ и даже не 100 000, а разрыв окупаемостей A и B на горизонте
# замера: сумма кабинета не изменилась ни на рубль. Замер Б: полоса
# перераспределения заявила 921 690 ₽ на 48 действиях — 16 % недельного
# расхода кабинета за неделю переносов.

import pytest

from sync.agent.writer import expectation

DAILY = {"A": 200_000.0, "B": 200_000.0, "C": 200_000.0}
HORIZON = 7


def _budget(object_id, delta_rub, roi=None, days=HORIZON, kind="budget.set"):
    """Действие, двигающее деньги кампании на delta_rub за окно замера."""
    action = {
        "idempotency_key": f"k-{object_id}",
        "action_kind": kind,
        "object_level": "campaign",
        "object_id": object_id,
        "exposure": {"daily_rub": abs(delta_rub) / days,
                     "basis": "сдвиг лимита"},
        "payload": {
            expectation.LEADS_KEY: 0.0,
            expectation.RUB_KEY: delta_rub,
            expectation.BASIS_KEY: "тестовое ожидание",
            expectation.DAYS_KEY: days,
        },
    }
    if roi is not None:
        action["marginal_roi"] = roi
    return action


def test_transfer_costs_the_efficiency_gap_not_the_amount():
    give = _budget("A", delta_rub=-100_000.0, roi=1.2)
    take = _budget("B", delta_rub=+100_000.0, roi=2.4)

    prices = risk_module.net_risk([give, take], DAILY, days=HORIZON)

    assert sum(prices.values()) < 100_000.0


def test_net_increase_pays_full_price():
    take = _budget("B", delta_rub=+100_000.0, roi=2.4)

    prices = risk_module.net_risk([take], DAILY, days=HORIZON)

    assert sum(prices.values()) == pytest.approx(100_000.0, rel=0.05)


def test_unknown_roi_gap_pays_full_price():
    # Та же дисциплина, что у object_daily_cost: неизвестное — не ноль.
    # Разрыв окупаемостей неизвестен, значит скидки за компенсацию нет.
    give = _budget("A", delta_rub=-100_000.0, roi=None)
    take = _budget("B", delta_rub=+100_000.0, roi=2.4)

    assert sum(risk_module.net_risk([give, take], DAILY, HORIZON).values()) >= 100_000.0


def test_transfer_between_equal_campaigns_is_nearly_free():
    # Окупаемости совпали — переносить деньги между ними не значит ничем
    # рисковать: ошибка ничего не меняет.
    give = _budget("A", delta_rub=-100_000.0, roi=2.0)
    take = _budget("B", delta_rub=+100_000.0, roi=2.0)

    assert sum(risk_module.net_risk([give, take], DAILY, HORIZON).values()) == 0.0


def test_neither_side_pays_the_transfer_twice():
    # Перенос — одна ошибка, а не две: пара платит за компенсированную часть
    # ОДИН раз, поровну, а не по полной цене каждой стороной.
    give = _budget("A", delta_rub=-100_000.0, roi=1.2)
    take = _budget("B", delta_rub=+100_000.0, roi=2.4)

    prices = risk_module.net_risk([give, take], DAILY, HORIZON)

    assert prices["k-A"] == prices["k-B"]
    # Разрыв 1.2 против 2.4 — половина: пара стоит 50 000, а не 200 000.
    assert sum(prices.values()) == pytest.approx(50_000.0, rel=0.01)


def test_only_the_compensated_part_is_discounted():
    # Такт даёт кабинету больше, чем забирает: разница — новые деньги под
    # ударом, и они платят как доливка, без всякой компенсации.
    give = _budget("A", delta_rub=-100_000.0, roi=1.2)
    take = _budget("B", delta_rub=+300_000.0, roi=2.4)

    total = sum(risk_module.net_risk([give, take], DAILY, HORIZON).values())

    # 200 000 нескомпенсированной доливки + 100 000 переноса с разрывом 0.5.
    assert total == pytest.approx(200_000.0 + 50_000.0, rel=0.01)


def test_hygiene_cut_does_not_subsidise_a_budget_increase():
    # Вырезанные гигиеной рубли — не карман, из которого доливка становится
    # дешевле: сломанный источник данных иначе делал бы любой прирост
    # бесплатным. Полоса гигиены риском не платит и скидки не даёт.
    cut = _budget("C", delta_rub=-100_000.0, roi=1.2, kind="negative.add")
    take = _budget("B", delta_rub=+100_000.0, roi=2.4)

    prices = risk_module.net_risk([cut, take], DAILY, HORIZON)

    assert prices["k-B"] == pytest.approx(100_000.0, rel=0.01)


def test_action_without_expectation_pays_its_gross_price():
    # Обещания нет — сколько денег такт двигает, неизвестно, и компенсации
    # не бывает: действие платит свою дельту целиком.
    lonely = _budget("B", delta_rub=+100_000.0, roi=2.4)
    lonely["payload"] = {}

    prices = risk_module.net_risk([lonely], DAILY, HORIZON)

    assert prices["k-B"] == pytest.approx(100_000.0, rel=0.01)


def test_undetermined_risk_stays_infinite_in_a_tick():
    # Справочник расхода пуст — цена неопределена, и никакая компенсация её
    # не превращает в конечное число: действие обязано уйти в отложенные.
    give = _budget("A", delta_rub=-100_000.0, roi=1.2)
    take = _budget("B", delta_rub=+100_000.0, roi=2.4)

    prices = risk_module.net_risk([give, take], {}, HORIZON)

    assert prices["k-A"] == float("inf")
    assert prices["k-B"] == float("inf")


def test_net_price_never_exceeds_the_gross_one():
    # Нетто-цена — скидка за взаимную компенсацию, а не наценка: ни одно
    # действие не должно подорожать оттого, что рядом оказалось другое.
    actions = [_budget("A", delta_rub=-40_000.0, roi=1.5),
               _budget("B", delta_rub=+120_000.0, roi=2.4)]

    prices = risk_module.net_risk(actions, DAILY, HORIZON)

    for action in actions:
        key = action["idempotency_key"]
        assert prices[key] <= action_risk(action, DAILY, HORIZON) + 0.01
