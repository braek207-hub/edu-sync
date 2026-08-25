# -*- coding: utf-8 -*-
"""Второй чекпоинт: вердикт по заявкам пересматривается деньгами.

Досрочное закрытие (EARLY_CLOSE_MIN_DAYS) выносит приговор по эффективным
лидам на седьмой день — и на этом действие уходит в историю обучения
навсегда. Но 91 % оплат приходят к 35-му дню, а edu_agent_facts зачисляет
оплату в день СОЗДАНИЯ лида (agent/facts.py: payments_fact растёт по
created_date). Значит то же самое окно наблюдения через месяц показывает
другую картину — уже денежную, — и вердикт по заявкам надо с ней сверить.
Без сверки петля обучения наполняется «успехами», которые деньгами не
подтверждены: тот же дефект, что аудит поймал у бинарного вердикта (C4).
"""

from datetime import date

from sync import agent_e1_watchdog as watchdog


def _facts(days, cost, payments, leads=10, start=date(2026, 8, 1)):
    out = []
    for i in range(days):
        out.append({"fact_date": date.fromordinal(start.toordinal() + i),
                    "cost": cost, "eff_leads": leads,
                    "payments_fact": payments})
    return out


def _action(baseline_cpo=None, applied="2026-07-01", verdict="improved"):
    red = {"baseline_cpa": 1000.0}
    if baseline_cpo is not None:
        red["baseline_cpo"] = baseline_cpo
    return {"action_id": "a1", "object_id": "111", "applied_at": applied,
            "red_line": red, "closing_verdict": verdict}


def test_money_metrics_count_payments_not_leads():
    window = (date(2026, 8, 1), date(2026, 8, 7), True)

    money = watchdog.money_metrics(_facts(7, cost=1000.0, payments=2), window)

    assert money["payments"] == 14
    assert money["cost"] == 7000.0
    assert money["cpo"] == 500.0


def test_no_payments_gives_no_price_instead_of_infinity():
    # Ноль оплат в окне — это «цены нет», а не «цена бесконечна»: последнее
    # пробило бы любой порог на первом же дне, как и у CPA.
    window = (date(2026, 8, 1), date(2026, 8, 7), True)

    money = watchdog.money_metrics(_facts(7, cost=1000.0, payments=0), window)

    assert money["cpo"] == 0.0


def test_money_confirms_the_leads_verdict():
    # Заявки подешевели, оплаты тоже — вердикт устоял.
    action = _action(baseline_cpo=5000.0)

    verdict = watchdog.money_verdict({"cpo": 3000.0, "payments": 40}, action)

    assert verdict == "improved"


def test_money_contradicts_the_leads_verdict():
    # Заявки подешевели, а оплаты подорожали: лиды пришли, но не платят.
    # Ровно тот случай, ради которого второй чекпоинт и нужен.
    action = _action(baseline_cpo=5000.0)

    verdict = watchdog.money_verdict({"cpo": 9000.0, "payments": 40}, action)

    assert verdict == "worsened"


def test_money_verdict_without_baseline_is_unknown_not_success():
    # У действий, применённых до появления денежной базы, сверять не с чем.
    # Молчаливое «improved» записало бы непроверенное как подтверждённое.
    action = _action(baseline_cpo=None)

    assert watchdog.money_verdict({"cpo": 3000.0, "payments": 40}, action) == "unknown"


def test_thin_volume_stays_inconclusive():
    # Две оплаты — не приговор: относительная ошибка больше эффекта.
    action = _action(baseline_cpo=5000.0)

    verdict = watchdog.money_verdict({"cpo": 4000.0, "payments": 2}, action)

    assert verdict == "inconclusive"


def test_checkpoint_is_due_only_after_the_maturation_horizon():
    today = date(2026, 8, 25)

    assert not watchdog.money_check_due(_action(applied="2026-08-01"), today)
    # 35 дней от применения — 91 % оплат дозрели (лаг CRM, память
    # edu-agent-economics-baseline).
    assert watchdog.money_check_due(_action(applied="2026-07-21"), today)


def test_checkpoint_is_not_due_without_an_application_date():
    # Строка без даты применения — не повод считать чекпоинт наступившим:
    # окно наблюдения отсчитывать не от чего.
    action = _action(applied=None)

    assert not watchdog.money_check_due(action, date(2026, 8, 25))


class _Journal:
    """Двойник журнала: отдаёт дозревшие строки и запоминает отметки."""

    def __init__(self, rows):
        self.rows = rows
        self.marked = []

    def actions_awaiting_money_check(self, days):
        return list(self.rows)

    def mark_money_checked(self, action_id, verdict):
        self.marked.append((action_id, verdict))
        return True


def _closed_action(by_leads, baseline_cpo=5000.0):
    return {"action_id": "a1", "object_id": "111",
            "applied_at": "2026-07-01", "created_at": "2026-07-01",
            "observation_verdict": by_leads,
            "red_line": {"baseline_cpa": 1000.0, "baseline_cpo": baseline_cpo}}


def test_contradiction_between_leads_and_money_reaches_the_report(monkeypatch):
    # Заявки подешевели, оплаты подорожали — то самое, ради чего чекпоинт.
    journal = _Journal([_closed_action("improved")])
    monkeypatch.setattr(watchdog, "load_facts",
                        lambda *a, **k: {"111": _facts(14, cost=10000.0, payments=1,
                                                       start=date(2026, 7, 2))})

    out = watchdog.money_checkpoint(journal, date(2026, 8, 25), date(2026, 8, 20), journal_ok=True)

    assert out["contradictions"] and out["contradictions"][0]["by_money"] == "worsened"
    assert journal.marked == [("a1", "worsened")]


def test_contradiction_raises_an_alarm():
    # Тревога обязана уметь уронить прогон: сторож по крону сообщает о себе
    # только красным раном, и молчаливая запись в отчёт равна молчанию.
    reasons = watchdog.alarm_reasons({
        "money_checkpoint": {"contradictions": [{"action_id": "a1"}]},
    })

    assert any("деньгам" in r for r in reasons)


def test_agreeing_verdicts_are_not_a_contradiction(monkeypatch):
    journal = _Journal([_closed_action("worsened")])
    monkeypatch.setattr(watchdog, "load_facts",
                        lambda *a, **k: {"111": _facts(14, cost=10000.0, payments=1,
                                                       start=date(2026, 7, 2))})

    out = watchdog.money_checkpoint(journal, date(2026, 8, 25), date(2026, 8, 20), journal_ok=True)

    assert out["contradictions"] == []
    assert journal.marked == [("a1", "worsened")]


def test_rehearsal_does_not_write_the_checkpoint(monkeypatch):
    # Сверка — утверждение о боевом кабинете: репетиция его не делает.
    journal = _Journal([_closed_action("improved")])
    monkeypatch.setattr(watchdog, "load_facts",
                        lambda *a, **k: {"111": _facts(14, cost=10000.0, payments=1,
                                                       start=date(2026, 7, 2))})

    out = watchdog.money_checkpoint(journal, date(2026, 8, 25), date(2026, 8, 20), journal_ok=False)

    assert journal.marked == []
    assert out["contradictions"], "исход виден и в репетиции, просто не записан"


def test_action_without_money_baseline_is_counted_not_hidden(monkeypatch):
    # Действия, применённые до появления денежной базы, сверить не с чем.
    # Их число обязано быть в отчёте: иначе «сверили всё» и «сверять было
    # нечем» выглядят одинаково.
    journal = _Journal([_closed_action("improved", baseline_cpo=None)])
    monkeypatch.setattr(watchdog, "load_facts",
                        lambda *a, **k: {"111": _facts(14, cost=10000.0, payments=1,
                                                       start=date(2026, 7, 2))})

    out = watchdog.money_checkpoint(journal, date(2026, 8, 25), date(2026, 8, 20), journal_ok=True)

    assert out["skipped_no_baseline"] == 1
    assert out["contradictions"] == []
