# -*- coding: utf-8 -*-
from sync.agent.writer.risk import action_risk, fit_into_budget, median_daily_cost, week_start


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
    actions = [{"idempotency_key": "a"}, {"idempotency_key": "b"}, {"idempotency_key": "c"}]
    risks = {"a": 4000.0, "b": 4000.0, "c": 4000.0}
    fits, deferred = fit_into_budget(actions, risks, remaining_rub=9000.0)
    assert [a["idempotency_key"] for a in fits] == ["a", "b"]
    assert [a["idempotency_key"] for a in deferred] == ["c"]


def test_fit_takes_nothing_when_budget_exhausted():
    actions = [{"idempotency_key": "a"}]
    fits, deferred = fit_into_budget(actions, {"a": 100.0}, remaining_rub=0.0)
    assert fits == []
    assert len(deferred) == 1


def test_fit_allows_all_when_budget_large():
    actions = [{"idempotency_key": "a"}, {"idempotency_key": "b"}]
    fits, deferred = fit_into_budget(actions, {"a": 1.0, "b": 1.0}, remaining_rub=1_000_000.0)
    assert len(fits) == 2
    assert deferred == []


def test_fit_defers_action_with_undetermined_risk():
    # Действие с неопределённым риском (+inf) не проходит бюджет ни при каком
    # конечном остатке — уходит в отложенные, а не пропускается бесплатно.
    actions = [{"idempotency_key": "unknown"}, {"idempotency_key": "known"}]
    risks = {"unknown": float("inf"), "known": 100.0}
    fits, deferred = fit_into_budget(actions, risks, remaining_rub=1_000_000.0)
    assert [a["idempotency_key"] for a in fits] == ["known"]
    assert [a["idempotency_key"] for a in deferred] == ["unknown"]
