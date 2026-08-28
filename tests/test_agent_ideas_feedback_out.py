# -*- coding: utf-8 -*-
"""
tests/test_agent_ideas_feedback_out.py — исход запущенной кампании
возвращается билдеру (задача 19 плана беты).

Петля замыкается здесь. Билдер уже умеет дообучаться на боевых поисковых
запросах (builder/feedback.py), но знает про кампанию только её расход и
клики. Расход — не исход: кампания, вынесшая связки, могла потратить ровно
столько же и при этом побить донорскую цену конверсии или провалить её, и
разница между этими двумя случаями решает, повторять ли вынос.

**Вердикт судит ровно тем критерием, который выписан в наряде.** Не «своей
формулой отчёта»: наряд объявил метрику, порог и базу сравнения заранее
(build_order.validate), и вердикт, посчитанный по другому правилу, был бы
оценкой задним числом.

**Молодая кампания — unknown, а не провал.** Горизонт наряда назван в нём же
и посчитан под порог значимости (power.MIN_EXPECTED_PAYMENTS). Кампания, не
дожившая до него, не «не справилась» — её просто ещё нечем судить, и назвать
это провалом значило бы закрывать идеи по нетерпению.

**Разбор фраз остаётся у получателя.** Агент отдаёт сырые строки запросов, а
классификацию (мусор / победители / маркеры) делает билдер своими порогами:
MIN_CLICKS_WASTE живёт в его коде, и вторая копия порога у отправителя
разъехалась бы с ним на первой же правке.
"""

import pytest

from sync.agent import power
from sync.agent.ideas import feedback_out


ACCOUNT = "account1-506453-ln8s"

# Горизонт наряда — 30 дней; порог значимости накоплен, если оплат ≥ 25.
HORIZON = 30


def _order(**over):
    order = {
        "order_id": "consolidate-vpo",
        "idea_id": "d96b5cf53b8073c1c6d122e5",
        "kind": "consolidate",
        "account": ACCOUNT,
        "level_slug": "vpo_consolidate",
        "campaign_name": "vpo / consolidate / consolidate-vpo",
        "direction": "vpo",
        "campaign": {"weekly_budget": 60_000, "target_cpa": 1_600,
                     "counter_id": 98_627_983, "goal_id": 360_811_375},
        "window_days": 30,
        "horizon_days": HORIZON,
        "success_rule": {"metric": "cpa_rub", "op": "<=", "threshold": 1_600.0,
                         "comparison": "vs_donors"},
    }
    order.update(over)
    return order


def _facts(days=HORIZON, cost=1_200.0, eff_leads=1, p_pay=1.0):
    """Дневные факты кампании наряда: ровно `days` дней открутки."""
    return [{"campaign_id": "555", "fact_date": f"2026-09-{day:02d}",
             "cost": cost, "eff_leads": eff_leads, "sum_p_pay": p_pay}
            for day in range(1, days + 1)]


def _queries():
    return [
        {"query": "колледж заочно москва", "clicks": 120, "cost_rub": 18_400.0,
         "conversions": 12},
        {"query": "колледж бесплатно скачать", "clicks": 40, "cost_rub": 5_100.0,
         "conversions": 0},
    ]


def _report(order=None, facts=None, **over):
    kwargs = {"applied_on": "2026-09-01", "today": "2026-10-05",
              "queries": _queries()}
    kwargs.update(over)
    return feedback_out.report(order or _order(),
                               _facts() if facts is None else facts, **kwargs)


# ------------------------------------------------------------------ вердикт


def test_launch_feedback_carries_the_verdict():
    # Шаг 1 задачи 19 дословно: отчёт несёт исход, а не только расход.
    out = _report()
    assert out["verdict"] in feedback_out.VERDICTS


def test_a_campaign_that_beat_the_donor_price_is_improved():
    # Критерий наряда: цена конверсии не выше донорской (1 600 ₽). Тридцать
    # дней по 1 200 ₽ и по одной конверсии — ровно 1 200 ₽ за конверсию.
    out = _report()
    assert out["verdict"] == "improved"
    assert out["fact"]["cpa_rub"] == pytest.approx(1_200.0)
    assert out["baseline"]["threshold"] == 1_600.0


def test_a_campaign_that_missed_the_donor_price_is_worsened():
    out = _report(facts=_facts(cost=2_000.0))
    assert out["verdict"] == "worsened"


def test_the_verdict_uses_the_rule_written_in_the_order():
    # Порог берётся из наряда, а не из настроек кампании и не из константы
    # отчёта: наряд объявил его заранее, и судить по другому числу значит
    # оценивать задним числом.
    out = _report(_order(success_rule={"metric": "cpa_rub", "op": "<=",
                                       "threshold": 900.0,
                                       "comparison": "vs_donors"}))
    assert out["verdict"] == "worsened"
    assert out["baseline"]["threshold"] == 900.0


# ------------------------------------------------ рано судить, а не провал


def test_a_campaign_younger_than_its_horizon_is_unknown():
    # Шаг 2 задачи 19. Двенадцать дней из тридцати — вердикта нет.
    out = _report(facts=_facts(days=12), today="2026-09-13")
    assert out["verdict"] == "unknown"
    assert out["days_live"] == 12
    assert "горизонт" in out["reason"]


def test_a_campaign_younger_than_its_horizon_is_not_a_failure_even_if_expensive():
    # Дорогая молодая кампания — всё ещё unknown: стратегия учится, и первые
    # дни у неё дороже по построению.
    out = _report(facts=_facts(days=5, cost=9_000.0), today="2026-09-06")
    assert out["verdict"] == "unknown"


def test_an_order_that_never_reached_the_cabinet_is_unknown():
    out = _report(facts=[], applied_on=None)
    assert out["verdict"] == "unknown"
    assert out["campaign_id"] is None


def test_a_campaign_with_no_spend_at_all_is_unknown():
    # Наряд уехал, кампания в кабинете есть, но стоит на паузе — её никто не
    # запускал. Это не провал выноса: судить нечего.
    out = _report(facts=[])
    assert out["verdict"] == "unknown"
    assert "не откручивалась" in out["reason"]


# ---------------------------------------------------------------- объём


def test_a_campaign_without_enough_payments_is_inconclusive():
    # Порог тот же, по которому генератор и планировал горизонт
    # (power.MIN_EXPECTED_PAYMENTS): своей ручки у отчёта нет.
    out = _report(facts=_facts(p_pay=0.1))
    assert out["verdict"] == "inconclusive"
    assert out["fact"]["expected_payments"] < power.MIN_EXPECTED_PAYMENTS


def test_spend_without_a_single_conversion_is_worsened_not_inconclusive():
    # Полный горизонт, деньги потрачены, конверсий ноль. Объёма для
    # статистики нет и не будет — но это факт, а не нехватка данных.
    out = _report(facts=_facts(eff_leads=0, p_pay=0.0))
    assert out["verdict"] == "worsened"
    assert "ни одной конверсии" in out["reason"]


# --------------------------------------------------- что уезжает билдеру


def test_the_report_names_the_level_the_builder_must_load():
    # Билдер адресует находки уровнем на диске. Слаг едет из наряда — тот же,
    # по которому уровень и собирался.
    assert _report()["level_slug"] == "vpo_consolidate"


def test_raw_queries_travel_unclassified():
    # Мусор от победителей отделяет ПОЛУЧАТЕЛЬ своим порогом
    # (builder.feedback.MIN_CLICKS_WASTE). Вторая копия порога здесь
    # разъехалась бы с ним при первой же правке.
    out = _report()
    assert {q["query"] for q in out["queries"]} == {
        "колледж заочно москва", "колледж бесплатно скачать"}
    assert "waste" not in out and "winners" not in out


def test_the_report_carries_the_link_back_to_the_idea():
    # Без idea_id вердикт некуда вернуть: реестр закрывает идею по нему.
    out = _report()
    assert out["idea_id"] == "d96b5cf53b8073c1c6d122e5"
    assert out["order_id"] == "consolidate-vpo"


def test_the_window_of_the_report_is_named():
    # «Цена конверсии 1 200 ₽» без окна — число без смысла. Отчёт обязан
    # сказать, за какой отрезок он посчитан.
    out = _report()
    assert out["window"]["from"] == "2026-09-01"
    assert out["window"]["to"] == "2026-09-30"


# ------------------------------------------- пример читается получателем


def test_the_shipped_example_matches_what_the_module_builds():
    """Пример в репозитории — то, что читает тест на стороне билдера.

    Проверяется не «файл существует», а совпадение с живым выходом модуля:
    разъедься они, билдер собирал бы приёмник под форму, которой агент уже
    не отдаёт.
    """
    import json
    import os

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "docs", "examples", "launch-feedback-consolidate.json")
    with open(path, encoding="utf-8") as f:
        example = json.load(f)
    assert set(example) == set(_report())
    assert example["verdict"] in feedback_out.VERDICTS


# ------------------------------------------------- обвязка: наряд из базы


def _wire(monkeypatch, idea, *, facts=None):
    from sync.agent.writer import db as writer_db

    monkeypatch.setattr(feedback_out.registry, "find_by_order",
                        lambda order_id, account=None:
                        idea if order_id == "consolidate-vpo" else None)
    monkeypatch.setattr(writer_db, "find_action_by_key",
                        lambda key: {"applied_at": "2026-09-01"})
    monkeypatch.setattr(feedback_out, "_facts_of",
                        lambda *a, **k: _facts() if facts is None else facts)
    monkeypatch.setattr(feedback_out, "_queries_of", lambda *a, **k: _queries())


def _idea_row(**over):
    idea = {"idea_id": "d96b5cf53b8073c1c6d122e5", "account": ACCOUNT,
            "status": "done",
            "action": {"kind": "campaign.create", "idempotency_key": "k-1",
                       "payload": {"order": _order()}}}
    idea.update(over)
    return idea


def test_for_order_builds_the_report_from_the_registry(monkeypatch):
    _wire(monkeypatch, _idea_row())
    out = feedback_out.for_order("consolidate-vpo", today="2026-10-05",
                                 account=ACCOUNT)
    assert out["verdict"] == "improved"
    assert out["window"]["from"] == "2026-09-01"


def test_a_closed_idea_still_returns_its_outcome(monkeypatch):
    # Идея, дожившая до конца горизонта, закрыта. Ищи мы её среди открытых —
    # исход не вернулся бы как раз у доведённых до конца выносов.
    _wire(monkeypatch, _idea_row(status="done"))
    assert feedback_out.for_order("consolidate-vpo", today="2026-10-05",
                                  account=ACCOUNT) is not None


def test_an_unknown_order_has_no_report(monkeypatch):
    _wire(monkeypatch, _idea_row())
    assert feedback_out.for_order("нет-такого", today="2026-10-05",
                                  account=ACCOUNT) is None
