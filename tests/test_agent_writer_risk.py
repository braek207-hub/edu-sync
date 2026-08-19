from sync.agent.writer.risk import action_risk, fit_into_budget, week_start


def test_week_start_is_monday():
    assert week_start("2026-08-19") == "2026-08-17"  # среда → понедельник


def test_risk_is_spend_until_measurement():
    # Цена ошибки = дневной расход объекта × дни до обнаружения.
    action = {"object_id": "111"}
    risk = action_risk(action, {"111": 1000.0}, days_to_measure=7)
    assert risk == 7000.0


def test_risk_zero_for_campaign_without_spend():
    assert action_risk({"object_id": "999"}, {"111": 1000.0}) == 0.0


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
