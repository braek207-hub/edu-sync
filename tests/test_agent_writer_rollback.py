# -*- coding: utf-8 -*-
import pytest

from sync.agent.writer.rollback import is_breached, red_line_for, rollback_payload

# Тестовый аварийный потолок: раньше на его месте молча подставлялся
# захардкоженный DEFAULT_ABSOLUTE_MAX_CPA=3000 из rollback.py — теперь
# параметр обязателен, значение выбирает вызывающий тест явно.
ABSOLUTE_MAX_CPA = 3000.0


def test_red_line_is_relative_to_baseline():
    line = red_line_for({"object_id": "111"}, baseline={"cpa": 1000.0},
                         absolute_max_cpa=ABSOLUTE_MAX_CPA)
    assert line["metric"] == "cpa"
    assert line["max_value"] == 1400.0     # +40% от базы
    assert line["min_leads"] == 20
    assert line["has_baseline"] is True


def test_red_line_for_requires_explicit_absolute_max_cpa():
    # Правка по код-ревью: DEFAULT_ABSOLUTE_MAX_CPA убран, absolute_max_cpa
    # обязателен. Забытый аргумент обязан упасть на вызове, а не тихо
    # подставить число, никак не связанное с экономикой кабинета.
    with pytest.raises(TypeError):
        red_line_for({"object_id": "111"}, baseline={})


def test_red_line_uses_absolute_threshold_when_no_baseline():
    # Новая/малонаблюдаемая кампания: базового CPA нет вовсе. Относительный
    # порог (проценты от нуля) не имеет смысла — красная линия обязана
    # остаться непустой и явно помеченной, а не тихо превратиться в 0.
    line = red_line_for({"object_id": "111"}, baseline={}, absolute_max_cpa=ABSOLUTE_MAX_CPA)
    assert line["metric"] == "cpa"
    assert line["has_baseline"] is False
    assert line["max_value"] > 0
    assert line["min_leads"] == 20


def test_red_line_uses_absolute_threshold_when_baseline_zero():
    # baseline.cpa == 0.0 — тот же случай "базы нет", что и пустой baseline:
    # база не отрицательная и не положительная, относительный порог не считается.
    line = red_line_for({"object_id": "111"}, baseline={"cpa": 0.0},
                         absolute_max_cpa=ABSOLUTE_MAX_CPA)
    assert line["has_baseline"] is False
    assert line["max_value"] > 0


def test_red_line_absolute_threshold_is_configurable():
    line = red_line_for({"object_id": "111"}, baseline={}, absolute_max_cpa=2500.0)
    assert line["has_baseline"] is False
    assert line["max_value"] == 2500.0


def test_breach_without_baseline_above_absolute_threshold():
    line = red_line_for({"object_id": "111"}, baseline={}, absolute_max_cpa=ABSOLUTE_MAX_CPA)
    breached, reason = is_breached(line, {"cpa": line["max_value"] + 500.0, "leads": 25})
    assert breached is True
    assert "недостаточно" not in reason.lower()


def test_no_breach_without_baseline_below_absolute_threshold():
    line = red_line_for({"object_id": "111"}, baseline={}, absolute_max_cpa=ABSOLUTE_MAX_CPA)
    breached, _ = is_breached(line, {"cpa": line["max_value"] - 100.0, "leads": 25})
    assert breached is False


def test_no_breach_before_minimum_leads_without_baseline():
    # Минимум наблюдений уважается и в безбазовом режиме: шум на новой
    # кампании не должен читаться как пробой аварийного порога.
    line = red_line_for({"object_id": "111"}, baseline={}, absolute_max_cpa=ABSOLUTE_MAX_CPA)
    breached, reason = is_breached(line, {"cpa": line["max_value"] + 1000.0, "leads": 3})
    assert breached is False
    assert "недостаточно" in reason.lower()


def test_breach_when_threshold_is_exactly_zero():
    # Регрессия: `if limit and value > limit` считал max_value=0.0 "порогом
    # не задан" и никогда не пробивался. 0.0 — валидный порог: пробивается
    # любым положительным наблюдаемым значением.
    line = {"metric": "cpa", "max_value": 0.0, "min_leads": 20}
    breached, reason = is_breached(line, {"cpa": 1.0, "leads": 25})
    assert breached is True
    assert "недостаточно" not in reason.lower()


def test_no_breach_when_threshold_not_set_at_all():
    # Противоположный случай той же регрессии: порог отсутствует в словаре
    # вовсе (а не равен 0) — тут действительно нельзя судить о пробое.
    line = {"metric": "cpa", "min_leads": 20}
    breached, reason = is_breached(line, {"cpa": 999999.0, "leads": 25})
    assert breached is False


def test_no_breach_before_minimum_leads():
    # До набора минимума наблюдений вывод делать нельзя: шум примут за провал.
    line = {"metric": "cpa", "max_value": 1400.0, "min_leads": 20}
    breached, reason = is_breached(line, {"cpa": 5000.0, "leads": 3})
    assert breached is False
    assert "недостаточно" in reason.lower()


def test_breach_when_metric_exceeds_line():
    line = {"metric": "cpa", "max_value": 1400.0, "min_leads": 20}
    breached, reason = is_breached(line, {"cpa": 1500.0, "leads": 25})
    assert breached is True
    assert "1500" in reason


def test_no_breach_when_within_line():
    line = {"metric": "cpa", "max_value": 1400.0, "min_leads": 20}
    breached, _ = is_breached(line, {"cpa": 1200.0, "leads": 25})
    assert breached is False


def test_rollback_restores_previous_percent():
    action = {"action_kind": "bidmodifier.set",
              "payload": {"Id": 7, "BidModifier": 30},
              "previous_state": {"Id": 7, "percent": 10}}
    service, method, params = rollback_payload(action)
    assert service == "bidmodifiers"
    assert method == "set"
    # previous_state.percent — дельта (+10 %), в API уходит 110.
    assert params["BidModifiers"][0]["BidModifier"] == 110


def test_rollback_of_add_disables_instead_of_deleting():
    # Удалять нельзя даже при откате: возвращаем НЕЙТРАЛЬНЫЙ коэффициент, но
    # только по Id из ответа API (result.AddResults[].Id) — Id никогда не
    # придумывается заранее. Нейтраль в шкале Директа = 100; ноль там означал
    # бы «ставка × 0», то есть удар сильнее исходного изменения.
    action = {"action_kind": "bidmodifier.add",
              "payload": {"CampaignId": 111, "Type": "MOBILE_ADJUSTMENT", "BidModifier": 30},
              "previous_state": {},
              "response": {"AddResults": [{"Id": 555}]}}
    service, method, params = rollback_payload(action)
    assert method == "set"
    assert params["BidModifiers"][0]["Id"] == 555
    assert params["BidModifiers"][0]["BidModifier"] == 100


def test_rollback_of_add_returns_none_when_id_unknown():
    # Id корректировки известен только из ответа API. Если действие ещё не
    # применялось (или ответ не сохранился) — откатывать вслепую по Id=0
    # нельзя: такого объекта не существует, запрос молча ничего не сделает.
    action = {"action_kind": "bidmodifier.add",
              "payload": {"CampaignId": 111, "Type": "MOBILE_ADJUSTMENT", "BidModifier": 30},
              "previous_state": {},
              "response": {}}
    assert rollback_payload(action) is None


def test_rollback_returns_none_without_previous_state_and_id():
    assert rollback_payload({"action_kind": "unknown", "payload": {}, "previous_state": {}}) is None
