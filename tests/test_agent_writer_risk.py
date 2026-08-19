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


def test_same_campaign_is_charged_once_per_run():
    # Две корректировки одной кампании: расход кампании один, сколько бы
    # корректировок ей ни ставили. Полная цена — первому действию, 0 — второму.
    actions = [_act("a", object_id="111"), _act("b", object_id="111")]
    risks = {"a": 7000.0, "b": 7000.0}

    fits, deferred = fit_into_budget(actions, risks, remaining_rub=10_000.0)

    assert [a["idempotency_key"] for a in fits] == ["a", "b"]
    assert deferred == []
    assert [a["risk_rub"] for a in fits] == [7000.0, 0.0]
    # Сумма, уходящая в журнал, равна цене ОДНОЙ кампании, а не двух.
    assert sum(a["risk_rub"] for a in fits) == 7000.0


def test_different_campaigns_are_charged_separately():
    actions = [_act("a", object_id="111"), _act("b", object_id="222")]
    risks = {"a": 7000.0, "b": 7000.0}

    fits, deferred = fit_into_budget(actions, risks, remaining_rub=10_000.0)

    assert [a["idempotency_key"] for a in fits] == ["a"]
    assert [a["idempotency_key"] for a in deferred] == ["b"]


def test_charged_objects_carry_across_calls_within_one_run():
    # Множество оплаченных объектов общее на прогон: кабинеты обрабатываются
    # по очереди, и второй вызов не должен снова платить за уже оплаченное.
    charged = set()
    fit_into_budget([_act("a", object_id="111")], {"a": 7000.0}, 10_000.0, charged)

    fits, _ = fit_into_budget([_act("b", object_id="111")], {"b": 7000.0}, 10_000.0, charged)

    assert fits[0]["risk_rub"] == 0.0


def test_deferred_action_does_not_mark_object_as_paid():
    # Действие, не влезшее в бюджет, объект не помечает — иначе следующее
    # действие по той же кампании прошло бы бесплатно за счёт так и не
    # применённого первого.
    charged = set()
    actions = [_act("a", object_id="111"), _act("b", object_id="111")]
    risks = {"a": 7000.0, "b": 7000.0}

    fits, deferred = fit_into_budget(actions, risks, remaining_rub=100.0, charged_objects=charged)

    assert fits == []
    assert len(deferred) == 2
    assert charged == set()
