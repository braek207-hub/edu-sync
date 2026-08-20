# -*- coding: utf-8 -*-
"""
tests/test_agent_e1_watchdog.py — сторож красных линий: наблюдение и автооткат.

Автооткат заменяет человеческий апрув, поэтому проверяется не «функция что-то
вернула», а поведение цикла: кто откатывается, кто нет, что уходит в кабинет и
что записывается в журнал. Чистые тесты — на фейках по протоколу
(.is_write_allowed()/.mutate(), mark_rolled_back/mark_rollback_failed), без
сети и без БД; живые — под гейтом DATABASE_URL, с уникальными ключами и
уборкой за собой (конвенция tests/test_agent_writer_db.py).
"""

import inspect
import os
import sys
import uuid
from datetime import date, datetime, timedelta

import pytest

import sync.agent_e1_watchdog as watchdog
import sync.agent.writer.db as writer_db
from sync.agent.writer.db import ensure_writer_tables, open_actions, spent_risk
from sync.db import get_connection

TODAY = date(2026, 8, 10)
APPLIED = datetime(2026, 8, 1, 12, 0)


def _action(**over):
    row = {
        "action_id": "act-1",
        "account": "cab",
        "object_level": "campaign",
        "object_id": "111",
        "action_kind": "bidmodifier.set",
        "payload": {"Id": 7, "BidModifier": 30},
        "previous_state": {"Id": 7, "percent": 10},
        "red_line": {"metric": "cpa", "max_value": 1000.0, "min_leads": 20,
                     "baseline_cpa": 714.0, "has_baseline": True},
        "status": "applied",
        "created_at": APPLIED - timedelta(hours=1),
        "applied_at": APPLIED,
        "response": {},
    }
    row.update(over)
    return row


def _facts(campaign_id, start, days, cost, leads):
    return [{"campaign_id": campaign_id,
             "fact_date": start + timedelta(days=i),
             "cost": cost, "eff_leads": leads}
            for i in range(days)]


class _FakeClient:
    """Протокол WriteClient в объёме, который нужен сторожу."""

    def __init__(self, dry_run=False, response=None, raises=None, sandbox=False,
                 modifiers=None):
        self.dry_run = dry_run
        self.sandbox = sandbox
        self.response = response if response is not None else {"SetResults": [{"Id": 7}]}
        self.raises = raises
        self.modifiers = modifiers
        self.calls = []
        self.reads = []
        self.units_left = None

    def is_write_allowed(self):
        return not self.dry_run

    def get(self, service, params):
        self.reads.append((service, params))
        if self.modifiers is None:
            raise RuntimeError("кабинет не отвечает")
        return {"BidModifiers": self.modifiers}

    def mutate(self, service, method, params):
        self.calls.append((service, method, params))
        if self.raises:
            raise self.raises
        return self.response


class _FakeDb:
    def __init__(self, failed_at=None):
        self.rolled_back = []
        self.failed = []
        self._failed_at = failed_at

    def mark_rolled_back(self, action_id):
        self.rolled_back.append(action_id)

    def mark_rollback_failed(self, action_id, reason, permanent=False):
        self.failed.append({"action_id": action_id, "reason": reason,
                            "permanent": permanent})
        return {"action_id": action_id, "rollback_attempts": len(self.failed),
                "rollback_failed_at": self._failed_at or (APPLIED if permanent else None)}


GREEN_GATE = {"status": "GREEN", "latest_fact_date": None, "reason": ""}
RED_GATE = {"status": "RED", "latest_fact_date": "2026-07-01",
            "reason": "витрина фактов не обновлялась"}


def _run(action, facts, client=None, db=None, holdout=(), today=TODAY,
         gate=GREEN_GATE):
    client = client or _FakeClient()
    db = db or _FakeDb()
    report = watchdog.watch(client, [action], db, {str(h) for h in holdout},
                            facts, today, gate)
    return report, client, db


# --------------------------------------------------------------- окно наблюдения

def test_window_starts_after_application_day_and_reserves_lead_lag():
    start, end, closed = watchdog.observation_window(_action(), TODAY)
    assert start == date(2026, 8, 2)   # день применения не наблюдаем
    # Верхняя граница отодвинута от сегодня на неполный день ПЛЮС запас под
    # лаг источника лидов: расход за вчера уже полон, а лиды ещё дозревают, и
    # лид-неполный день завышает наблюдаемый CPA всегда в одну сторону.
    assert watchdog.LEADS_LAG_DAYS >= 1
    assert end == TODAY - timedelta(days=1 + watchdog.LEADS_LAG_DAYS)
    assert end == date(2026, 8, 7)
    assert closed is False


def test_window_is_capped_by_horizon():
    start, end, closed = watchdog.observation_window(_action(), date(2026, 9, 1))
    assert start == date(2026, 8, 2)
    assert end == date(2026, 8, 2) + timedelta(days=watchdog.OBSERVATION_HORIZON_DAYS - 1)
    assert closed is True


def test_window_is_none_until_first_full_day_passed():
    assert watchdog.observation_window(_action(), date(2026, 8, 2)) is None


def test_window_falls_back_to_created_at_for_row_without_applied_at():
    action = _action(applied_at=None, created_at=datetime(2026, 8, 3, 9, 0))
    start, _, _ = watchdog.observation_window(action, TODAY)
    assert start == date(2026, 8, 4)


def test_observed_metrics_counts_only_days_inside_window():
    rows = _facts("111", date(2026, 8, 1), 12, cost=100.0, leads=5)
    observed = watchdog.observed_metrics(rows, (date(2026, 8, 2), date(2026, 8, 9), False))
    assert observed["days"] == 8
    assert observed["cost"] == 800.0
    assert observed["leads"] == 40
    assert observed["cpa"] == 20.0


def test_observed_cpa_is_zero_without_leads_instead_of_infinity():
    rows = _facts("111", date(2026, 8, 2), 3, cost=9000.0, leads=0)
    observed = watchdog.observed_metrics(rows, (date(2026, 8, 2), date(2026, 8, 9), False))
    assert observed["cpa"] == 0.0


# --------------------------------------------------------------- вердикты

def test_action_below_min_leads_is_not_rolled_back():
    # CPA катастрофический, но наблюдений 8 из 20 — вывод о провале делать
    # нельзя: иначе шум примут за провал и откатят здоровое изменение.
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=9000.0, leads=1)}
    report, client, db = _run(_action(), facts)

    assert report["states"] == {watchdog.STATE_COLLECTING: 1}
    assert report["breached"] == 0
    assert client.calls == []
    assert db.rolled_back == [] and db.failed == []


def test_breached_action_is_rolled_back():
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    report, client, db = _run(_action(), facts)

    assert report["states"] == {watchdog.STATE_BREACHED: 1}
    assert report["rolled_back"] == 1
    assert db.rolled_back == ["act-1"]
    service, method, params = client.calls[0]
    assert (service, method) == ("bidmodifiers", "set")
    # Прошлое состояние хранится дельтой (+10 %), в API уходит 100-база.
    assert params == {"BidModifiers": [{"Id": 7, "BidModifier": 110}]}


def test_healthy_action_stays_under_watch():
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=100.0, leads=5)}
    report, client, db = _run(_action(), facts)

    assert report["states"] == {watchdog.STATE_WATCHED: 1}
    assert client.calls == []


def test_application_day_and_today_never_enter_the_verdict():
    # Разгон в день применения и неполные сегодняшние сутки не должны решать
    # судьбу изменения: оба дня лежат вне окна.
    facts = {"111": (
        _facts("111", date(2026, 8, 1), 1, cost=100000.0, leads=0)     # день применения
        + _facts("111", date(2026, 8, 2), 8, cost=100.0, leads=5)      # окно
        + _facts("111", date(2026, 8, 10), 1, cost=100000.0, leads=0)  # сегодня
    )}
    report, client, _ = _run(_action(), facts)

    assert report["states"] == {watchdog.STATE_WATCHED: 1}
    assert client.calls == []


def test_closed_horizon_without_min_leads_is_reported_as_expired():
    facts = {"111": _facts("111", date(2026, 8, 2), 14, cost=100.0, leads=1)}
    report, client, _ = _run(_action(), facts, today=date(2026, 9, 1))

    assert report["states"] == {watchdog.STATE_EXPIRED: 1}
    assert client.calls == []


def test_action_without_red_line_is_not_judged():
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=9000.0, leads=10)}
    report, client, db = _run(_action(red_line={}), facts)

    assert report["states"] == {watchdog.STATE_NO_RED_LINE: 1}
    assert client.calls == []
    assert db.rolled_back == []


def test_window_not_open_yet_is_reported_as_waiting():
    report, client, _ = _run(_action(), {}, today=date(2026, 8, 2))
    assert report["states"] == {watchdog.STATE_WAITING: 1}
    assert client.calls == []


# --------------------------------------------------------------- откат

def test_rollback_without_known_id_sends_nothing():
    # bidmodifier.add: Id приходит только в ответе API. Ответа нет — откат
    # вслепую невозможен, запрос отправлять нельзя.
    action = _action(action_kind="bidmodifier.add",
                     payload={"CampaignId": 111, "BidModifier": 30,
                              "Type": "MOBILE_ADJUSTMENT", "key": "MOBILE"},
                     previous_state={}, response={})
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    report, client, db = _run(action, facts)

    assert client.calls == []
    assert report["rollback_failed"] == 1
    assert db.failed[0]["permanent"] is True
    assert "Id" in db.failed[0]["reason"]


def test_rollback_of_added_modifier_sets_neutral_and_never_deletes():
    action = _action(action_kind="bidmodifier.add",
                     payload={"CampaignId": 111, "BidModifier": 30,
                              "Type": "MOBILE_ADJUSTMENT", "key": "MOBILE"},
                     previous_state={},
                     response={"AddResults": [{"Id": 4242}]})
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    report, client, db = _run(action, facts)

    assert report["rolled_back"] == 1
    service, method, params = client.calls[0]
    assert method == "set"  # не delete: агент не удаляет объекты никогда
    assert params == {"BidModifiers": [{"Id": 4242, "BidModifier": 100}]}


def test_dry_run_sends_nothing_and_touches_no_journal():
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    client = _FakeClient(dry_run=True)
    report, client, db = _run(_action(), facts, client=client)

    assert report["breached"] == 1
    assert report["would_roll_back"] == 1
    assert report["rolled_back"] == 0
    assert client.calls == []
    assert db.rolled_back == [] and db.failed == []


def test_dry_run_does_not_mark_unrollbackable_action_in_journal():
    action = _action(action_kind="bidmodifier.add",
                     payload={"CampaignId": 111, "BidModifier": 30,
                              "Type": "MOBILE_ADJUSTMENT", "key": "MOBILE"},
                     previous_state={}, response={})
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    report, client, db = _run(action, facts, client=_FakeClient(dry_run=True))

    assert report["rollback_failed"] == 1   # видно в отчёте
    assert db.failed == []                  # но репетиция журнал не меняет


def test_rollback_request_passes_guardrails(monkeypatch):
    # Путь отката обязан проходить те же рельсы, что прямое применение.
    monkeypatch.setattr(watchdog, "rollback_payload",
                        lambda action: ("bidmodifiers", "delete",
                                        {"BidModifiers": [{"Id": 7, "BidModifier": 100}]}))
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    report, client, db = _run(_action(), facts)

    assert client.calls == []
    assert report["rollback_failed"] == 1
    assert db.failed[0]["permanent"] is True
    assert "рельсы" in db.failed[0]["reason"]


def test_incomplete_rollback_request_is_not_sent(monkeypatch):
    # Тело возврата без Id — это тот же откат вслепую, только собранный
    # ошибкой в построителе запроса, а не отсутствием Id в журнале.
    monkeypatch.setattr(watchdog, "rollback_payload",
                        lambda action: ("bidmodifiers", "set",
                                        {"BidModifiers": [{"BidModifier": 110}]}))
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    report, client, db = _run(_action(), facts)

    assert client.calls == []
    assert report["rollback_failed"] == 1
    assert db.failed[0]["permanent"] is True


def test_rollback_guard_form_carries_api_coefficient_not_delta():
    # Рельса пути возврата считает в 100-базной шкале API и проверяет диапазон
    # Директа, а не потолок назначения. Поле названо api_coefficient, чтобы
    # две рельсы в разных единицах нельзя было перепутать молча.
    form = watchdog.rollback_guard_form(_action(), "bidmodifiers", "set",
                                        {"BidModifiers": [{"Id": 7, "BidModifier": 180}]})
    assert form["api_coefficient"] == 180
    assert form["action_kind"] == "bidmodifier.set"
    assert form["payload"]["Id"] == 7


def test_rollback_to_legitimate_past_value_above_assignment_cap_is_sent():
    # Человек когда-то поставил +80 % — штатное значение Директа. Агент
    # перекрыл его своим, линия пробита, возврат обязан пройти: потолок
    # НАЗНАЧЕНИЯ ±50 % описывает решения агента, а не чужое прошлое значение.
    action = _action(previous_state={"Id": 7, "percent": 80})
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    report, client, db = _run(action, facts)

    assert report["rolled_back"] == 1
    assert db.failed == []
    assert client.calls[0][2] == {"BidModifiers": [{"Id": 7, "BidModifier": 180}]}


def test_rollback_to_disabled_segment_is_sent():
    # Прошлое состояние «показы на устройстве выключены» — это коэффициент 0
    # в шкале Директа и -100 в дельтах. Потолок назначения его не пропускал.
    action = _action(previous_state={"Id": 7, "percent": -100})
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    report, client, db = _run(action, facts)

    assert report["rolled_back"] == 1
    assert client.calls[0][2] == {"BidModifiers": [{"Id": 7, "BidModifier": 0}]}


def test_guardrails_reject_rollback_outside_direct_api_range(monkeypatch):
    # Значение вне диапазона API Директа — отказ верен и на пути возврата:
    # такой элемент либо будет отклонён, либо применит не то, что задумано.
    monkeypatch.setattr(watchdog, "rollback_payload",
                        lambda action: ("bidmodifiers", "set",
                                        {"BidModifiers": [{"Id": 7, "BidModifier": 1400}]}))
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    report, client, db = _run(_action(), facts)

    assert client.calls == []
    assert report["rollback_failed"] == 1
    assert db.failed[0]["permanent"] is True
    assert "диапазон" in db.failed[0]["reason"].lower()


def test_unrestorable_past_state_is_not_sent():
    # previous_state вне диапазона Директа вовсе не собирается в запрос:
    # delta_to_api роняет построение, и повтор его не починит.
    action = _action(previous_state={"Id": 7, "percent": 1250})
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    report, client, db = _run(action, facts)

    assert client.calls == []
    assert report["rollback_failed"] == 1
    assert db.failed[0]["permanent"] is True


def test_holdout_campaign_is_never_touched():
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    report, client, db = _run(_action(), facts, holdout=("111",))

    assert client.calls == []
    assert report["blocked_holdout"] == 1
    assert report["rolled_back"] == 0
    # Пометки неоткатываемости нет: состав заповедника меняется.
    assert db.failed == []


def test_send_failure_is_recorded_but_not_permanent():
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    client = _FakeClient(raises=RuntimeError("сеть недоступна"))
    report, client, db = _run(_action(), facts, client=client)

    assert report["rollback_failed"] == 1
    assert db.rolled_back == []
    assert db.failed[0]["permanent"] is False
    assert "сеть недоступна" in db.failed[0]["reason"]


def test_element_error_in_response_is_not_treated_as_rolled_back():
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    client = _FakeClient(response={"SetResults": [{"Errors": [{"Code": 8800}]}]})
    report, client, db = _run(_action(), facts, client=client)

    assert db.rolled_back == []
    assert report["rollback_failed"] == 1
    assert db.failed[0]["permanent"] is False


def test_report_counts_actions_under_watch_and_failures():
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4),
             "222": _facts("222", date(2026, 8, 2), 8, cost=100.0, leads=5)}
    healthy = _action(action_id="act-2", object_id="222")
    client, db = _FakeClient(), _FakeDb()

    report = watchdog.watch(client, [_action(), healthy], db, set(), facts, TODAY,
                            GREEN_GATE)

    assert report["under_watch"] == 2
    assert report["states"] == {watchdog.STATE_BREACHED: 1, watchdog.STATE_WATCHED: 1}
    assert report["rolled_back"] == 1
    assert report["breached_sample"][0]["object_id"] == "111"


def test_broken_journal_row_does_not_blind_the_watchdog():
    # Испорченная строка журнала не должна отменять наблюдение по остальным:
    # непойманное исключение здесь означало бы, что ни одно пробившее линию
    # изменение в этом прогоне не откатится.
    broken = _action(action_id="broken", object_id="333",
                     red_line={"metric": "cpa", "max_value": 100.0, "min_leads": "мусор"})
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4),
             "333": _facts("333", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    client, db = _FakeClient(), _FakeDb()

    report = watchdog.watch(client, [broken, _action()], db, set(), facts, TODAY,
                            GREEN_GATE)

    assert report["errors"] == 1
    assert report["errors_sample"][0]["object_id"] == "333"
    assert report["rolled_back"] == 1          # здоровое действие рассужено
    assert db.rolled_back == ["act-1"]


def test_facts_window_covers_every_action_window():
    old = _action(action_id="old", applied_at=datetime(2026, 7, 20, 10, 0))
    span = watchdog.facts_window([old, _action()], TODAY)
    assert span == (date(2026, 7, 21), date(2026, 8, 7))


def test_facts_window_is_none_when_no_action_is_observable_yet():
    assert watchdog.facts_window([_action()], date(2026, 8, 2)) is None


# ------------------------------------------- окружение: песочница vs боевой

def test_sandbox_with_write_is_refused():
    # Песочница боевым логинам недоступна: вызов падает «логин не подключен»,
    # неудача засчитывается — и уходит в БОЕВОЙ журнал, база одна. Три таких
    # «безопасных» прогона снимают действие с наблюдения навсегда.
    assert watchdog.refusal(sandbox=True, dry_run=False)


def test_other_flag_combinations_are_allowed():
    assert watchdog.refusal(sandbox=True, dry_run=True) is None     # репетиция
    assert watchdog.refusal(sandbox=False, dry_run=True) is None    # боевая репетиция
    assert watchdog.refusal(sandbox=False, dry_run=False) is None   # боевой откат


def test_main_refuses_sandbox_apply_before_touching_the_database(monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise AssertionError("отказ обязан случиться ДО обращения к БД")

    monkeypatch.setattr(sys, "argv", ["agent_e1_watchdog", "--apply"])
    monkeypatch.setattr(watchdog.writer_db, "ensure_writer_tables", boom)
    monkeypatch.setattr(watchdog.writer_db, "open_actions", boom)

    code = watchdog.main()

    assert code != 0
    assert "REFUSED" in capsys.readouterr().out


def test_sandbox_client_never_writes_to_the_prod_journal():
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    client = _FakeClient(sandbox=True, dry_run=False,
                         raises=RuntimeError("логин не подключен"))
    report, client, db = _run(_action(), facts, client=client)

    assert report["rollback_failed"] == 1            # видно в отчёте
    assert db.failed == []                           # но боевой журнал цел
    assert report["failures"][0]["journal_written"] is False


def test_sandbox_success_is_not_recorded_as_rolled_back():
    # Откат прошёл в ПЕСОЧНИЦЕ. В боевом кабинете изменение осталось живым —
    # отметка «откатано» в боевом журнале была бы прямой ложью.
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    client = _FakeClient(sandbox=True, dry_run=False)
    report, client, db = _run(_action(), facts, client=client)

    assert client.calls != []
    assert db.rolled_back == []


# ------------------------------------------------- гейт качества данных

def test_empty_window_is_not_treated_as_healthy():
    # Данных нет — это не «проблемы нет». Пустое окно даёт нули по всем
    # метрикам, и «нулей нет выше порога» читалось бы как здоровье: красная
    # линия молча не срабатывала бы никогда.
    report, client, _ = _run(_action(), {})

    assert report["states"] == {watchdog.STATE_LOW_COVERAGE: 1}
    assert client.calls == []


def test_gappy_window_is_not_judged():
    # Два дня фактов из шести: вердикт по такому окну — вердикт по случайной
    # выборке дней. Витрину наполняет agent_e0, у которого нет расписания.
    facts = {"111": _facts("111", date(2026, 8, 2), 2, cost=5000.0, leads=15)}
    report, client, _ = _run(_action(), facts)

    assert report["states"] == {watchdog.STATE_LOW_COVERAGE: 1}
    assert client.calls == []


def test_low_coverage_at_closed_horizon_goes_to_manual_review():
    facts = {"111": _facts("111", date(2026, 8, 2), 2, cost=5000.0, leads=15)}
    report, client, db = _run(_action(), facts, today=date(2026, 9, 1))

    assert report["states"] == {watchdog.STATE_EXPIRED: 1}
    assert report["needs_review"] == 1
    assert db.failed[0]["permanent"] is True


def test_breach_caused_only_by_the_last_day_waits_for_confirmation():
    # Пять здоровых дней и лид-неполный последний: расход есть, лиды ещё не
    # доехали. На полном окне линия пробита, без последнего дня — нет.
    # Вердикт берётся каждый прогон по РАСТУЩЕМУ окну, и выигрывает первое
    # пересечение порога, поэтому пробой обязан прожить два прогона.
    facts = {"111": (_facts("111", date(2026, 8, 2), 5, cost=1000.0, leads=5)
                     + _facts("111", date(2026, 8, 7), 1, cost=30000.0, leads=0))}
    report, client, db = _run(_action(), facts)

    assert report["states"] == {watchdog.STATE_UNCONFIRMED: 1}
    assert report["rolled_back"] == 0
    assert client.calls == []
    assert db.rolled_back == []


def test_red_data_gate_watches_but_does_not_roll_back():
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    report, client, db = _run(_action(), facts, gate=RED_GATE)

    assert report["breached"] == 1                # наблюдение показано
    assert report["blocked_data_gate"] == 1       # но в кабинет прогон не пишет
    assert report["rolled_back"] == 0
    assert client.calls == []
    assert db.rolled_back == [] and db.failed == []


def test_missing_gate_forbids_rollback():
    # Умолчание — запрет: забытый гейт не должен молча разрешать откат по
    # данным неизвестного качества.
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    report, client, _ = _run(_action(), facts, gate=None)

    assert report["blocked_data_gate"] == 1
    assert client.calls == []


def test_facts_gate_is_red_when_mart_is_stale():
    facts = {"111": _facts("111", date(2026, 7, 20), 3, cost=100.0, leads=1)}
    gate = watchdog.facts_gate(facts, TODAY)

    assert gate["status"] == "RED"
    assert gate["latest_fact_date"] == "2026-07-22"


def test_facts_gate_is_red_when_mart_is_empty():
    assert watchdog.facts_gate({}, TODAY)["status"] == "RED"


def test_facts_gate_is_green_on_fresh_mart():
    facts = {"111": _facts("111", TODAY - timedelta(days=3), 3, cost=100.0, leads=1)}
    assert watchdog.facts_gate(facts, TODAY)["status"] == "GREEN"


def test_load_facts_asks_the_mart_up_to_today(monkeypatch):
    # Свежесть витрины видна только за верхней границей окна: окно отодвинуто
    # от сегодня на запас под лаг лидов. Спрашивай мы ровно окно — «витрина
    # мертва неделю» и «мы спросили только про старые дни» были бы неотличимы.
    seen = {}

    def fake_load(ids, date_from, date_to):
        seen.update({"from": date_from, "to": date_to})
        return []

    monkeypatch.setattr(watchdog.agent_db, "load_daily_facts", fake_load)
    watchdog.load_facts([_action()], TODAY)

    assert seen["from"] == "2026-08-02"
    assert seen["to"] == TODAY.isoformat()


# ------------------------------------- состояния без вердикта: ручной разбор

def test_expired_action_is_closed_and_counted_for_manual_review():
    facts = {"111": _facts("111", date(2026, 8, 2), 14, cost=100.0, leads=1)}
    report, client, db = _run(_action(), facts, today=date(2026, 9, 1))

    assert report["states"] == {watchdog.STATE_EXPIRED: 1}
    assert report["needs_review"] == 1
    assert report["needs_review_sample"][0]["object_id"] == "111"
    assert db.failed[0]["permanent"] is True   # уходит из наблюдения навсегда
    assert db.rolled_back == []                # изменение живо: его смотрит человек
    assert client.calls == []


def test_action_without_red_line_is_closed_for_manual_review():
    report, client, db = _run(_action(red_line={}), {})

    assert report["states"] == {watchdog.STATE_NO_RED_LINE: 1}
    assert report["needs_review"] == 1
    assert db.failed[0]["permanent"] is True


def test_rehearsal_reports_needs_review_without_touching_the_journal():
    report, client, db = _run(_action(red_line={}), {}, client=_FakeClient(dry_run=True))

    assert report["needs_review"] == 1
    assert db.failed == []


# ------------------------------- восстановление Id застрявшего «добавления»

_CABINET_MOBILE = [{"Id": 4242, "CampaignId": 111, "Type": "MOBILE_ADJUSTMENT",
                    "MobileAdjustment": {"BidModifier": 130}}]


def _stuck_add():
    return _action(action_kind="bidmodifier.add",
                   payload={"CampaignId": 111, "BidModifier": 30,
                            "Type": "MOBILE_ADJUSTMENT", "key": "MOBILE"},
                   previous_state={}, response={})


def test_added_modifier_id_is_recovered_from_the_cabinet():
    # Строка 'stale': запрос ушёл, ответ не сохранился, Id созданного объекта
    # неизвестен — и действие было неоткатываемым по построению. Но Id
    # восстановим: bidmodifiers.get отдаёт его для каждой пары «тип, ключ».
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    report, client, db = _run(_stuck_add(), facts,
                              client=_FakeClient(modifiers=_CABINET_MOBILE))

    assert report["rolled_back"] == 1
    assert client.calls[0][2] == {"BidModifiers": [{"Id": 4242, "BidModifier": 100}]}


def test_ambiguous_cabinet_match_is_never_guessed():
    twins = _CABINET_MOBILE + [{"Id": 9999, "CampaignId": 111,
                                "Type": "MOBILE_ADJUSTMENT",
                                "MobileAdjustment": {"BidModifier": 120}}]
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    report, client, db = _run(_stuck_add(), facts, client=_FakeClient(modifiers=twins))

    assert len(client.reads) == 1        # кабинет прочитан
    assert client.calls == []            # но угадывать между двумя Id нельзя
    assert report["rollback_failed"] == 1
    assert db.failed[0]["permanent"] is True


def test_unreachable_cabinet_does_not_invent_an_id():
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    report, client, db = _run(_stuck_add(), facts, client=_FakeClient(modifiers=None))

    assert len(client.reads) == 1
    assert client.calls == []
    assert report["rollback_failed"] == 1


def test_recovery_does_not_match_a_different_segment():
    cabinet = [{"Id": 555, "CampaignId": 111, "Type": "DESKTOP_ADJUSTMENT",
                "DesktopAdjustment": {"BidModifier": 130}}]
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    report, client, db = _run(_stuck_add(), facts, client=_FakeClient(modifiers=cabinet))

    assert len(client.reads) == 1
    assert client.calls == []
    assert report["rollback_failed"] == 1


# --------------------------------------------------------------- журнал: SQL

def test_open_actions_skips_rolled_back_and_unrollbackable_rows():
    source = inspect.getsource(writer_db.open_actions)
    assert "rolled_back_at IS NULL" in source
    assert "rollback_failed_at IS NULL" in source


def test_writer_ddl_adds_rollback_failure_columns():
    ddl = "\n".join(writer_db.WRITER_DDL)
    assert "ADD COLUMN IF NOT EXISTS rollback_attempts" in ddl
    assert "ADD COLUMN IF NOT EXISTS rollback_failed_at" in ddl
    assert "ALTER TABLE edu_agent_actions" in ddl


# --------------------------------------------------------------- живые тесты

live_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL")


def _journal_row(key: str, account: str, **over):
    row = {
        "idempotency_key": key,
        "account": account,
        "object_level": "campaign",
        "object_id": "111",
        "action_kind": "bidmodifier.set",
        "payload": {"Id": 7, "BidModifier": 30},
        "previous_state": {"Id": 7, "percent": 10},
        "red_line": {"metric": "cpa", "max_value": 1000.0, "min_leads": 20},
        "risk_rub": 100.0,
    }
    row.update(over)
    return row


def _cleanup(*keys) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM edu_agent_actions WHERE idempotency_key = ANY(%s)", (list(keys),))
        conn.commit()


@live_db
def test_live_rolled_back_action_is_not_picked_up_again():
    ensure_writer_tables()
    suffix = uuid.uuid4().hex[:8]
    key = "test-watchdog-rolled-" + suffix
    try:
        action_id = writer_db.insert_action(_journal_row(key, "test-" + suffix))
        writer_db.mark_action(action_id, "applied", {"SetResults": [{"Id": 7}]})
        assert action_id in {a["action_id"] for a in open_actions()}

        writer_db.mark_rolled_back(action_id)

        assert action_id not in {a["action_id"] for a in open_actions()}
    finally:
        _cleanup(key)


@live_db
def test_live_unrollbackable_action_leaves_watch_but_keeps_paying_risk():
    # Откат не удался — изменение всё ещё живёт в кабинете. Повторять
    # обречённый запрос каждый прогон нельзя, но и снимать за него плату с
    # риск-бюджета нельзя: деньги под ним никуда не делись.
    ensure_writer_tables()
    suffix = uuid.uuid4().hex[:8]
    key = "test-watchdog-failed-" + suffix
    week = (date.today() - timedelta(days=1)).isoformat()
    try:
        action_id = writer_db.insert_action(_journal_row(key, "test-" + suffix))
        writer_db.mark_action(action_id, "applied", {})
        before_risk = writer_db.spent_risk(week)
        before_manual = writer_db.failed_rollbacks_count()

        marked = writer_db.mark_rollback_failed(action_id, "нет Id", permanent=True)

        assert marked["rollback_failed_at"] is not None
        assert marked["rollback_attempts"] == 1
        assert action_id not in {a["action_id"] for a in open_actions()}
        assert writer_db.spent_risk(week) == before_risk
        assert writer_db.failed_rollbacks_count() == before_manual + 1
    finally:
        _cleanup(key)


@live_db
def test_live_transient_failure_retries_until_attempt_limit():
    ensure_writer_tables()
    suffix = uuid.uuid4().hex[:8]
    key = "test-watchdog-retry-" + suffix
    try:
        action_id = writer_db.insert_action(_journal_row(key, "test-" + suffix))
        writer_db.mark_action(action_id, "applied", {})

        seen = []
        for _ in range(writer_db.MAX_ROLLBACK_ATTEMPTS):
            row = writer_db.mark_rollback_failed(action_id, "сеть недоступна")
            seen.append(row["rollback_failed_at"])

        assert seen[0] is None                    # первый сбой не хоронит действие
        assert seen[-1] is not None               # но повторы не бесконечны
        assert action_id not in {a["action_id"] for a in open_actions()}
    finally:
        _cleanup(key)


@live_db
def test_live_spent_risk_still_counts_row_awaiting_manual_rollback():
    ensure_writer_tables()
    suffix = uuid.uuid4().hex[:8]
    key = "test-watchdog-risk-" + suffix
    week = (date.today() - timedelta(days=1)).isoformat()
    try:
        action_id = writer_db.insert_action(
            _journal_row(key, "test-" + suffix, risk_rub=777.0))
        writer_db.mark_action(action_id, "applied", {})
        with_action = spent_risk(week)
        writer_db.mark_rollback_failed(action_id, "нет Id", permanent=True)
        assert spent_risk(week) == with_action

        writer_db.mark_rolled_back(action_id)
        assert spent_risk(week) == with_action - 777.0
    finally:
        _cleanup(key)
