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
import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta

import pytest

import sync.agent_e1_watchdog as watchdog
import sync.agent.writer.db as writer_db
from sync.agent.writer.db import ensure_writer_tables, open_actions, spent_risk
from sync.agent.writer.rollback import is_spend_collapsed
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
    def __init__(self, failed_at=None, rolled_back_taken=False,
                 observation_taken=False):
        self.rolled_back = []
        self.failed = []
        self.harmful = []
        self.observation_closed = []
        self.experiments = []
        self._failed_at = failed_at
        # True — строку журнала между вердиктом и отметкой забрал другой
        # контур: реальный UPDATE с гардом её не тронет и вернёт False.
        self._rolled_back_taken = rolled_back_taken
        self._observation_taken = observation_taken

    def mark_rolled_back(self, action_id):
        self.rolled_back.append(action_id)
        return not self._rolled_back_taken

    def mark_observation_closed(self, action_id, verdict, leads_delta=None):
        self.observation_closed.append((action_id, verdict, leads_delta))
        return not self._observation_taken

    def record_experiments(self, rows):
        self.experiments.extend(rows)
        return len(rows)

    def mark_harmful_verdict(self, action_id, reason):
        self.harmful.append({"action_id": action_id, "reason": reason})
        return True

    def mark_rollback_failed(self, action_id, reason, permanent=False):
        self.failed.append({"action_id": action_id, "reason": reason,
                            "permanent": permanent})
        return {"action_id": action_id, "rollback_attempts": len(self.failed),
                "rollback_failed_at": self._failed_at or (APPLIED if permanent else None)}


GREEN_GATE = {"status": "GREEN", "latest_fact_date": None, "reason": ""}
RED_GATE = {"status": "RED", "latest_fact_date": "2026-07-01",
            "reason": "витрина фактов не обновлялась"}


def _mart(facts):
    """Витрина, какой её увидел бы отдельный запрос по ВСЕЙ витрине.

    В бою знаменатель покрытия и ширину гейта считает
    agent_db.load_mart_day_breadth — по всем кампаниям витрины, без фильтра
    по кампаниям открытых действий. В тестах «вся витрина» это ровно те
    кампании, которые тест положил в facts, поэтому структура собирается из
    них: так фикстуры остаются в одном месте. Что в БОЮ источник другой —
    отдельный запрос по всей витрине без фильтра по наблюдаемым кампаниям —
    проверяют тесты «дефект И5» ниже по файлу.
    """
    days = {}
    campaigns = set()
    for campaign_id, rows in (facts or {}).items():
        campaigns.add(str(campaign_id))
        for row in rows:
            day = row.get("fact_date")
            days.setdefault(day, set()).add(str(campaign_id))
    return {"days": {d: len(c) for d, c in days.items()},
            "campaigns_total": len(campaigns)}


# Граница зрелости CRM по умолчанию: столько же, сколько давал прежний
# фиксированный отступ (сегодня минус три дня). Так проверки, написанные до
# перехода на границу из данных, продолжают описывать то же самое окно, а
# новые — задают crm_through явно.
DEFAULT_CRM_THROUGH = TODAY - timedelta(days=3)


def _run(action, facts, client=None, db=None, holdout=(), today=TODAY,
         gate=GREEN_GATE, mart=None, crm_through=None):
    client = client or _FakeClient()
    db = db or _FakeDb()
    report = watchdog.watch(client, [action], db, {str(h) for h in holdout},
                            facts, today, gate,
                            today - timedelta(days=3) if crm_through is None
                            else crm_through,
                            None, _mart(facts) if mart is None else mart)
    return report, client, db


# --------------------------------------------------------------- окно наблюдения

def test_window_starts_after_application_day_and_reserves_lead_lag():
    start, end, closed = watchdog.observation_window(_action(), TODAY, TODAY - timedelta(days=3))
    assert start == date(2026, 8, 2)   # день применения не наблюдаем
    # Верхняя граница отодвинута от сегодня на неполный день ПЛЮС запас под
    # лаг источника лидов: расход за вчера уже полон, а лиды ещё дозревают, и
    # лид-неполный день завышает наблюдаемый CPA всегда в одну сторону.
    assert watchdog.LEADS_LAG_DAYS >= 1
    assert end == TODAY - timedelta(days=1 + watchdog.LEADS_LAG_DAYS)
    assert end == date(2026, 8, 7)
    assert closed is False


def test_window_is_capped_by_horizon():
    start, end, closed = watchdog.observation_window(_action(), date(2026, 9, 1),
                                                     date(2026, 9, 1) - timedelta(days=3))
    assert start == date(2026, 8, 2)
    assert end == date(2026, 8, 2) + timedelta(days=watchdog.OBSERVATION_HORIZON_DAYS - 1)
    assert closed is True


def test_window_is_none_until_first_full_day_passed():
    assert watchdog.observation_window(_action(), date(2026, 8, 2),
                                       date(2026, 8, 2) - timedelta(days=3)) is None


def test_window_falls_back_to_created_at_for_row_without_applied_at():
    action = _action(applied_at=None, created_at=datetime(2026, 8, 3, 9, 0))
    start, _, _ = watchdog.observation_window(action, TODAY, TODAY - timedelta(days=3))
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
                            GREEN_GATE, DEFAULT_CRM_THROUGH, None, _mart(facts))

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
                            GREEN_GATE, DEFAULT_CRM_THROUGH, None, _mart(facts))

    assert report["errors"] == 1
    assert report["errors_sample"][0]["object_id"] == "333"
    assert report["rolled_back"] == 1          # здоровое действие рассужено
    assert db.rolled_back == ["act-1"]


def test_facts_window_covers_every_action_window():
    old = _action(action_id="old", applied_at=datetime(2026, 7, 20, 10, 0))
    span = watchdog.facts_window([old, _action()], TODAY, DEFAULT_CRM_THROUGH)
    assert span == (date(2026, 7, 21), date(2026, 8, 7))


def test_facts_window_is_none_when_no_action_is_observable_yet():
    assert watchdog.facts_window([_action()], date(2026, 8, 2),
                                     date(2026, 8, 2) - timedelta(days=3)) is None


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
    gate = watchdog.facts_gate(_mart(facts), TODAY)

    assert gate["status"] == "RED"
    assert gate["latest_fact_date"] == "2026-07-22"


def test_facts_gate_is_red_when_mart_is_empty():
    assert watchdog.facts_gate(_mart({}), TODAY)["status"] == "RED"


def test_facts_gate_is_green_on_fresh_mart():
    facts = {"111": _facts("111", TODAY - timedelta(days=3), 3, cost=100.0, leads=1)}
    assert watchdog.facts_gate(_mart(facts), TODAY)["status"] == "GREEN"


def test_load_facts_asks_the_mart_up_to_today(monkeypatch):
    # Свежесть витрины видна только за верхней границей окна: окно отодвинуто
    # от сегодня на запас под лаг лидов. Спрашивай мы ровно окно — «витрина
    # мертва неделю» и «мы спросили только про старые дни» были бы неотличимы.
    seen = {}

    def fake_load(ids, date_from, date_to):
        seen.update({"from": date_from, "to": date_to})
        return []

    monkeypatch.setattr(watchdog.agent_db, "load_daily_facts", fake_load)
    watchdog.load_facts([_action()], TODAY, DEFAULT_CRM_THROUGH)

    assert seen["from"] == "2026-08-02"
    assert seen["to"] == TODAY.isoformat()


# ------------------------------------- дефект И5: источник знаменателя ширины

def test_mart_breadth_is_asked_for_the_whole_mart_not_for_watched_campaigns(monkeypatch):
    """Ширина витрины — отдельный запрос БЕЗ фильтра по кампаниям действий.

    Дефект И5: знаменатель «большинства кампаний» брался из фактов кампаний,
    по которым открыты действия. На первом боевом прогоне их одна-две — гейт
    превращался в термометр одной кампании и объявлял витрину здоровой,
    пока жива она. Запрос обязан идти по всей витрине.
    """
    seen = {}

    def fake_breadth(date_from, date_to):
        seen.update({"args": (date_from, date_to)})
        # витрина шире наблюдаемых кампаний — так и должно быть
        return {"days": {"2026-08-10": 84}, "campaigns_total": 84}

    monkeypatch.setattr(watchdog.agent_db, "load_mart_day_breadth", fake_breadth)
    out = watchdog.load_mart_breadth([_action()], TODAY, DEFAULT_CRM_THROUGH)

    # Ни идентификаторов кампаний, ни коллекций в аргументах — только даты.
    assert seen["args"] == ("2026-08-02", TODAY.isoformat())
    # Ответ витрины возвращается как есть: 84 кампании, а не одна наблюдаемая.
    assert out["campaigns_total"] == 84
    assert out["days"] == {"2026-08-10": 84}


def test_mart_breadth_is_empty_when_there_is_nothing_to_watch(monkeypatch):
    # Нет открытых действий — нет и окна: запрос не должен уходить с
    # выдуманными границами, а гейт получает честно пустую витрину.
    def boom(*args, **kwargs):
        raise AssertionError("запрос ширины без окна наблюдения")

    monkeypatch.setattr(watchdog.agent_db, "load_mart_day_breadth", boom)
    assert watchdog.load_mart_breadth([], TODAY, TODAY - timedelta(days=3)) == {"days": {}, "campaigns_total": 0}


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


# =========================================================================
# Дефект 2: сторож работает без аренды прогона
# =========================================================================
#
# Механизм аренды был, имя сторожа лежало в реестре — а сам он аренду не брал.
# Два одновременных сторожа откатывают одно действие дважды и дважды списывают
# попытки (MAX_ROLLBACK_ATTEMPTS = 3: двух параллельных прогонов хватает,
# чтобы похоронить строку за один заход). Старый тест проверял, что имя есть
# в списке, — он зеленел ровно всё время, пока аренда не бралась.


class _RecordingLock:
    """Аренда на прогон, которая записывает, кто и под каким именем её взял."""

    def __init__(self, busy=False, lease=None):
        self.busy = busy
        self.names = []
        self.lease = lease

    def __call__(self, name, *a, **k):
        self.names.append(name)
        lock = self

        class _Ctx:
            def __enter__(self_inner):
                if lock.busy:
                    raise writer_db.RunLockBusy("прогон уже идёт")
                return lock.lease

            def __exit__(self_inner, *exc):
                return False

        return _Ctx()


def _patch_watchdog_main(monkeypatch, lock):
    monkeypatch.setattr(sys, "argv", ["agent_e1_watchdog", "--prod"])
    monkeypatch.setattr(watchdog.writer_db, "ensure_writer_tables", lambda: None)
    monkeypatch.setattr(watchdog.writer_db, "run_lock", lock)
    monkeypatch.setattr(watchdog.writer_db, "open_actions", lambda: [])
    monkeypatch.setattr(watchdog.writer_db, "failed_rollbacks_count", lambda: 0)
    monkeypatch.setattr(watchdog.agent_db, "load_holdout_ids", lambda: [])
    # Граница зрелости CRM — запрос к БД на каждом прогоне: без подмены тест
    # аренды падал бы на отсутствии DATABASE_URL, а не на своём утверждении.
    monkeypatch.setattr(watchdog.agent_db, "crm_maturity_date",
                        lambda: DEFAULT_CRM_THROUGH)
    # Второй чекпоинт спрашивает журнал о дозревших строках на каждом
    # прогоне — тем же доводом, что и граница зрелости выше: тест про аренду
    # обязан падать на своём утверждении, а не на живой базе.
    monkeypatch.setattr(watchdog.writer_db, "actions_awaiting_money_check",
                        lambda days: [])


def test_watchdog_takes_the_run_lease_under_its_own_name(monkeypatch, capsys):
    lock = _RecordingLock()
    _patch_watchdog_main(monkeypatch, lock)

    assert watchdog.main() == 0
    capsys.readouterr()

    assert lock.names == ["agent_writer"], (
        "аренда обязана быть ВЗЯТА, под ОБЩИМ с прямым применением именем: "
        "e1 и сторож пишут в один кабинет и не должны идти параллельно")


def test_second_watchdog_does_not_start(monkeypatch, capsys):
    # Второй сторож откатил бы то же действие второй раз и вторично списал бы
    # попытку — журнал бы решил, что откат не удаётся.
    lock = _RecordingLock(busy=True)
    _patch_watchdog_main(monkeypatch, lock)
    monkeypatch.setattr(watchdog.writer_db, "open_actions",
                        lambda: (_ for _ in ()).throw(
                            AssertionError("занятая аренда обязана остановить прогон ДО работы")))

    code = watchdog.main()

    assert code == 1
    assert "RUN_LOCKED" in capsys.readouterr().out


# =========================================================================
# Дефект 3: неудавшийся откат не доходил до планировщика
# =========================================================================


def test_breach_is_recorded_even_when_rollback_fails():
    # Линия пробита, откатить не смогли (Id корректировки неизвестен). Строка
    # снимается с наблюдения — но сегмент от этого не стал безопасным, и
    # планировщик обязан узнать о вердикте: иначе процент дрейфует на пункт,
    # ключ идемпотентности получается новый, и агент назавтра крутит тот же
    # сегмент ещё раз, поверх живого вредного изменения.
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    action = _action(action_kind="bidmodifier.add",
                     payload={"CampaignId": 111, "Type": "MOBILE_ADJUSTMENT",
                              "key": "MOBILE", "BidModifier": 30},
                     previous_state={}, response={})
    report, client, db = _run(action, facts)

    assert report["rollback_failed"] == 1
    assert db.rolled_back == []
    assert [h["action_id"] for h in db.harmful] == ["act-1"]


def test_breach_verdict_is_recorded_before_a_successful_rollback():
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    report, client, db = _run(_action(), facts)

    assert report["rolled_back"] == 1
    assert [h["action_id"] for h in db.harmful] == ["act-1"]


def test_healthy_action_is_never_marked_harmful():
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=100.0, leads=5)}
    report, client, db = _run(_action(), facts)

    assert db.harmful == []


def test_breach_on_red_data_gate_is_not_recorded_as_harmful():
    # Гейт витрины красный: пробой показан, но данным доверять нельзя — ни
    # откатывать, ни запирать сегмент на два месяца по ним не будем.
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    report, client, db = _run(_action(), facts, gate=RED_GATE)

    assert report["breached"] == 1
    assert report["blocked_data_gate"] == 1
    assert db.harmful == []


def test_rehearsal_does_not_record_the_verdict_in_the_journal():
    # То же правило журнала, что и везде: репетиция боевых записей не меняет.
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    report, client, db = _run(_action(), facts, client=_FakeClient(dry_run=True))

    assert db.harmful == []


# =========================================================================
# Дефект 4: переход «откатано» писался без гарда
# =========================================================================


def test_rollback_whose_journal_row_was_taken_is_not_reported_as_rolled_back():
    # Возврат в кабинет прошёл, но строку журнала уже закрыл другой контур.
    # «Отметили» и «строку забрал кто-то другой» — разные исходы, и отчёт
    # обязан их различать, а не утверждать про журнал то, чего в нём нет.
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    db = _FakeDb(rolled_back_taken=True)
    report, client, db = _run(_action(), facts, db=db)

    assert report["rolled_back"] == 0
    assert report["conflicted"] == 1
    assert report["conflicted_sample"][0]["reason"] == watchdog.JOURNAL_CONFLICT_REASON


def test_rollback_marked_in_the_journal_is_reported_as_rolled_back():
    # Обратная половина: нормальный путь обязан остаться нормальным, иначе
    # проверка выше зеленела бы на коде, который вообще ничего не отмечает.
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    report, client, db = _run(_action(), facts)

    assert report["rolled_back"] == 1
    assert report["conflicted"] == 0


# =========================================================================
# Дефект 5: аренда перепроверяется и перед откатом
# =========================================================================


class _LostLease:
    def guard(self):
        raise writer_db.RunLeaseLost("аренда потеряна")


def test_lost_lease_stops_the_watchdog_before_it_writes():
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    client, db = _FakeClient(), _FakeDb()

    with pytest.raises(writer_db.RunLeaseLost):
        watchdog.watch(client, [_action()], db, set(), facts, TODAY, GREEN_GATE,
                       DEFAULT_CRM_THROUGH, _LostLease(), _mart(facts))

    assert client.calls == [], "после потери аренды откат в кабинет не уходит"


# =========================================================================
# Дефект Г1: подтверждение пробоя не фильтровало выброс
# =========================================================================
#
# Прежняя проверка требовала, чтобы пробой держался «на том же окне без
# последнего дня». Окно НАКОПИТЕЛЬНОЕ — растёт от даты применения, — поэтому
# «окно без последнего дня» есть буквально окно предыдущего прогона: для
# накопительной средней пробой персистентен по построению. Разовый дорогой
# день, продавивший порог сегодня, продавливал его и назавтра, и здоровая
# кампания откатывалась на втором прогоне. Теперь пробой обязан пережить
# выброс: он проверяется на окне БЕЗ дня с максимальным расходом.

def _healthy_with_one_expensive_day():
    """Пять здоровых дней, один дорогой без лидов, дальше снова здоровые."""
    return {"111": (
        _facts("111", date(2026, 8, 2), 5, cost=1000.0, leads=5)      # здоровые
        + _facts("111", date(2026, 8, 7), 1, cost=30000.0, leads=0)   # выброс
        + _facts("111", date(2026, 8, 8), 5, cost=1000.0, leads=5)    # снова здоровые
    )}


def test_single_expensive_day_never_becomes_a_confirmed_breach():
    # Прогон 2: окно 02–08 августа, выброс уже НЕ последний день.
    facts = _healthy_with_one_expensive_day()
    report, client, db = _run(_action(), facts, today=date(2026, 8, 11))

    assert report["states"] == {watchdog.STATE_UNCONFIRMED: 1}
    assert report["rolled_back"] == 0
    assert client.calls == []
    assert db.rolled_back == [] and db.failed == []


def test_old_confirmation_rule_would_have_rolled_this_campaign_back():
    # Та же кампания и тот же прогон глазами прежнего правила: «окно без
    # последнего дня» — это окно вчерашнего прогона, и пробой на нём есть.
    # Тест держит дефект зафиксированным: почини мы его переименованием, а не
    # по существу, здесь было бы видно.
    rows = _healthy_with_one_expensive_day()["111"]
    red_line = _action()["red_line"]
    window = watchdog.observation_window(_action(), date(2026, 8, 11),
                                         date(2026, 8, 11) - timedelta(days=3))
    start, end, closed = window

    full = watchdog.observed_metrics(rows, window)
    without_last_day = watchdog.observed_metrics(rows, (start, end - timedelta(days=1), closed))
    without_peak = watchdog.observed_metrics(
        rows, window, exclude=watchdog.peak_cost_day(rows, window))

    assert watchdog.is_breached(red_line, full)[0] is True              # порог пробит
    assert watchdog.is_breached(red_line, without_last_day)[0] is True  # прежнее «подтверждение»
    assert watchdog.is_breached(red_line, without_peak)[0] is False     # выброс снят — пробоя нет


def test_expensive_day_is_visible_in_the_report():
    # Отчёт обязан показывать, ЧТО именно не подтвердилось: иначе «сторож
    # молчит» неотличимо от «всё хорошо».
    facts = _healthy_with_one_expensive_day()
    verdict = watchdog.judge(_action(), facts, date(2026, 8, 11), _mart(facts),
                             date(2026, 8, 11) - timedelta(days=3))

    assert verdict["without_peak_day"]["date"] == "2026-08-07"
    assert verdict["without_peak_day"]["cost"] == 6000.0
    assert "2026-08-07" in verdict["reason"]


def test_persistent_degradation_is_still_rolled_back():
    # Обратная половина: фильтр выброса не должен глушить настоящую
    # деградацию. Она размазана по дням и переживает снятие любого одного.
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    report, client, db = _run(_action(), facts)

    assert report["states"] == {watchdog.STATE_BREACHED: 1}
    assert report["rolled_back"] == 1
    assert "без самого дорогого дня" in report["breached_sample"][0]["reason"]


def test_breach_carried_by_two_expensive_days_survives_the_filter():
    # Фильтр снимает ОДИН день, а не «все дорогие»: два дорогих дня подряд —
    # уже не выброс, и вердикт обязан состояться.
    facts = {"111": (_facts("111", date(2026, 8, 2), 4, cost=1000.0, leads=6)
                     + _facts("111", date(2026, 8, 6), 2, cost=30000.0, leads=0))}
    report, client, db = _run(_action(), facts)

    assert report["states"] == {watchdog.STATE_BREACHED: 1}
    assert report["rolled_back"] == 1


# =========================================================================
# Э2.4: петля обучения — исход действия уходит в историю экспериментов
# =========================================================================


def _held_facts():
    """Полное окно горизонта: линия не пробита, лидов больше минимума.

    Окно действия — 02.08–15.08 (горизонт 14 дней), сегодня 01.09: горизонт
    закрыт. CPA наблюдения 350 при базе 714 и пределе 1000.
    """
    return {"111": _facts("111", date(2026, 8, 2), 14, cost=700.0, leads=2)}


def test_held_action_at_closed_horizon_becomes_experiment_and_leaves_watch():
    report, client, db = _run(_action(), _held_facts(), today=date(2026, 9, 1))

    assert report["states"] == {watchdog.STATE_WATCHED: 1}
    assert report["closed_held"] == 1
    assert report["closed_held_sample"][0]["object_id"] == "111"
    # Вердикт градуированный: CPA вдвое ниже базы — это «улучшило», а не
    # безразличное «не пробило порог» (аудит C4).
    # Темпа базы в линии этого действия нет — факта не существует, и в
    # журнал едет None, а не ноль: «не измерено» и «эффекта не было» петля
    # обучения обязана различать.
    assert db.observation_closed == [("act-1", "improved", None)]
    assert db.failed == [] and db.rolled_back == []
    assert client.calls == []          # закрытие — не запись в кабинет

    exp = db.experiments[0]
    assert exp["source"] == "action"
    assert exp["verdict"] == "improved"
    assert exp["metric"] == "eff_cpl"
    assert exp["mechanism"] == "before_after"
    # Без контроля сезон не вычтен — класс B, не A.
    assert exp["reliability_class"] == "B"
    assert abs(exp["effect"] - (350.0 - 714.0) / 714.0) < 1e-3
    assert exp["params"]["rel_error_floor"] is True


def test_held_closure_is_deferred_on_red_gate():
    # Вердикт «выдержало» по мёртвой витрине не выносится: нули неотличимы
    # от здоровья — та же логика, что у закрытия строк без вердикта.
    report, client, db = _run(_action(), _held_facts(), today=date(2026, 9, 1),
                              gate=RED_GATE)

    assert report["closed_held"] == 0
    assert report["closing_deferred"] == 1
    assert db.observation_closed == []
    assert db.experiments == []


def test_rehearsal_reports_would_close_but_does_not_touch_journal():
    report, client, db = _run(_action(), _held_facts(), today=date(2026, 9, 1),
                              client=_FakeClient(dry_run=True))

    assert report["would_close_held"] == 1
    assert report["closed_held"] == 0
    assert db.observation_closed == []
    assert db.experiments == []


def test_row_taken_by_another_watchdog_does_not_emit_second_experiment():
    report, client, db = _run(_action(), _held_facts(), today=date(2026, 9, 1),
                              db=_FakeDb(observation_taken=True))

    assert report["closed_held"] == 0
    assert db.experiments == []


def test_rolled_back_action_writes_breached_experiment():
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    report, client, db = _run(_action(), facts)

    assert report["rolled_back"] == 1
    assert len(db.experiments) == 1
    assert db.experiments[0]["source"] == "action"
    assert db.experiments[0]["verdict"] == "breached"
    assert db.experiments[0]["object_id"] == "111"


def test_action_experiment_id_is_deterministic_and_effect_needs_baseline():
    window = (date(2026, 8, 2), date(2026, 8, 15), True)
    observed = {"cpa": 350.0, "leads": 28}
    a = watchdog.action_experiment(_action(), observed, window, "held")
    b = watchdog.action_experiment(_action(), observed, window, "held")
    assert a["experiment_id"] == b["experiment_id"]

    # Красная линия без базы (has_baseline=False): сравнивать не с чем,
    # эффект честно пуст, но исход всё равно записан.
    no_base = watchdog.action_experiment(
        _action(red_line={"metric": "cpa", "max_value": 3000.0,
                          "min_leads": 20, "baseline_cpa": 0.0,
                          "has_baseline": False}),
        observed, window, "held")
    assert no_base["effect"] is None
    assert no_base["verdict"] == "held"


# =========================================================================
# Дефект Г2: мёртвая витрина хоронила действие навсегда
# =========================================================================


def test_expired_action_is_not_buried_while_the_data_gate_is_red():
    # Цепочка «расчёт перестали запускать → покрытие низкое каждый прогон →
    # горизонт закрылся» помечала действие непроверяемым НАВСЕГДА — по
    # причине «данных не было». Перманентная пометка на красном гейте
    # ставиться не должна.
    facts = {"111": _facts("111", date(2026, 8, 2), 2, cost=5000.0, leads=15)}
    report, client, db = _run(_action(), facts, today=date(2026, 9, 1), gate=RED_GATE)

    assert report["states"] == {watchdog.STATE_EXPIRED: 1}
    assert db.failed == []                       # журнал не тронут
    assert report["needs_review"] == 0
    assert report["closing_deferred"] == 1       # но состояние видно в отчёте
    assert report["closing_deferred_sample"][0]["object_id"] == "111"
    assert report["closing_deferred_sample"][0]["reason"] == watchdog.GATE_DEFERS_CLOSING_REASON


def test_action_without_red_line_is_not_buried_on_a_red_gate_either():
    report, client, db = _run(_action(red_line={}), {}, gate=RED_GATE)

    assert db.failed == []
    assert report["closing_deferred"] == 1


def test_missing_gate_also_defers_the_permanent_mark():
    # Умолчание — запрет, ровно как у отката.
    facts = {"111": _facts("111", date(2026, 8, 2), 2, cost=5000.0, leads=15)}
    report, client, db = _run(_action(), facts, today=date(2026, 9, 1), gate=None)

    assert db.failed == []
    assert report["closing_deferred"] == 1


def test_green_gate_still_closes_the_unverified_action():
    # Обратная половина: на здоровой витрине «вердикта не будет» — правда, и
    # строка обязана уйти человеку, а не висеть в наблюдении вечно.
    facts = {"111": _facts("111", date(2026, 8, 2), 2, cost=5000.0, leads=15)}
    report, client, db = _run(_action(), facts, today=date(2026, 9, 1))

    assert report["needs_review"] == 1
    assert report["closing_deferred"] == 0
    assert db.failed[0]["permanent"] is True


# =========================================================================
# Дефект Г3: покрытие окна считалось не от того знаменателя
# =========================================================================


def test_campaign_with_intermittent_delivery_still_gets_a_verdict():
    # Витрина наполняется парами «день, кампания», реально присутствующими в
    # источниках: дня с нулевой откруткой в ней нет вовсе. Считая покрытие по
    # дням САМОЙ кампании, сторож требовал от неё ежедневной открутки — и чем
    # сильнее правка агента придушила кампанию, тем вернее вердикта не будет.
    intermittent = [
        {"campaign_id": "111", "fact_date": day, "cost": 8000.0, "eff_leads": 7}
        for day in (date(2026, 8, 2), date(2026, 8, 3), date(2026, 8, 5), date(2026, 8, 7))
    ]
    facts = {"111": intermittent,
             # витрина за окно наполнена: расчёт отработал все шесть дней
             "222": _facts("222", date(2026, 8, 2), 6, cost=100.0, leads=5)}
    report, client, db = _run(_action(), facts)

    assert report["states"] == {watchdog.STATE_BREACHED: 1}
    assert report["rolled_back"] == 1
    # По прежнему знаменателю это 4 дня из 6 — 67 % при минимуме 80 %.
    assert len(intermittent) / 6 < watchdog.MIN_WINDOW_COVERAGE


def test_coverage_is_measured_on_the_mart_not_on_the_campaign():
    window = (date(2026, 8, 2), date(2026, 8, 7), False)
    facts = {"111": _facts("111", date(2026, 8, 2), 2, cost=10.0, leads=1),
             "222": _facts("222", date(2026, 8, 2), 6, cost=10.0, leads=1)}

    assert watchdog.mart_filled_days(_mart(facts), window) == 6
    assert watchdog.window_coverage(watchdog.mart_filled_days(_mart(facts), window), window) == 1.0


def test_dead_mart_is_still_low_coverage():
    # Обратная половина: если витрины нет ни по одной кампании, покрытия нет
    # ни у кого — вердикт по нулям не выносится.
    report, client, _ = _run(_action(), {"222": _facts("222", date(2026, 8, 2), 2,
                                                       cost=10.0, leads=1)})

    assert report["states"] == {watchdog.STATE_LOW_COVERAGE: 1}
    assert client.calls == []


# =========================================================================
# Дефект Г4: рельса возврата проверяла форму, но не намерение
# =========================================================================


def test_rollback_to_a_value_that_is_not_the_past_state_is_refused(monkeypatch):
    # 1300 — предел шкалы Директа, то есть «в диапазоне»: прежняя рельса
    # пропустила бы его насквозь, и агент вкрутил бы ставку сегмента в потолок
    # под видом отмены. Путь возврата больше ничем не проверен.
    monkeypatch.setattr(watchdog, "rollback_payload",
                        lambda action: ("bidmodifiers", "set",
                                        {"BidModifiers": [{"Id": 7, "BidModifier": 1300}]}))
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    report, client, db = _run(_action(), facts)

    assert client.calls == []
    assert report["rollback_failed"] == 1
    assert "рельсы" in db.failed[0]["reason"]
    assert "110" in db.failed[0]["reason"]


def test_rollback_guard_form_carries_the_journal_past_state():
    # Рельса выводит ожидаемый коэффициент САМА из полей журнала: посчитай его
    # сторож — сверка проверяла бы построитель запроса им же самим.
    form = watchdog.rollback_guard_form(_action(), "bidmodifiers", "set",
                                        {"BidModifiers": [{"Id": 7, "BidModifier": 110}]})

    assert form["origin_action_kind"] == "bidmodifier.set"
    assert form["previous_state"] == {"Id": 7, "percent": 10}


def test_correct_rollback_still_passes_the_rail():
    # Обратная половина: сверка намерения не должна ломать нормальный откат.
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=5000.0, leads=4)}
    report, client, db = _run(_action(), facts)

    assert report["rolled_back"] == 1
    assert client.calls[0][2] == {"BidModifiers": [{"Id": 7, "BidModifier": 110}]}


# =========================================================================
# Мелкое: умолчания, которые молчат
# =========================================================================


def test_client_without_sandbox_flag_is_a_broken_client_not_a_prod_one():
    # Оба умолчания плохие: «боевой» открывает боевой журнал прогону
    # неизвестного окружения, «песочный» — молча выключает журнал боевому.
    class _NoFlag:
        def is_write_allowed(self):
            return True

    with pytest.raises(AttributeError):
        watchdog.journal_allowed(_NoFlag())


def test_gate_is_red_when_the_whole_mart_stopped():
    # Частичный отказ расчёта = отстала ВСЯ витрина, а не одна кампания.
    # Раньше порог считался от объединения кампаний за окно, и такой день
    # не набирался никогда: за две недели в витрине копятся все кампании,
    # что хоть раз откручивались, а в отдельный день активна лишь часть.
    # Гейт вставал в вечный отказ, и сторож не откатывал НИКОГДА.
    facts = {"111": _facts("111", date(2026, 7, 20), 3, cost=100.0, leads=1),
             "222": _facts("222", date(2026, 7, 20), 3, cost=100.0, leads=1)}
    gate = watchdog.facts_gate(_mart(facts), TODAY)

    assert gate["status"] == "RED"
    assert "расчёт" in gate["reason"]


def test_gate_is_red_when_the_freshest_day_is_too_narrow():
    # Свежесть по дате ещё не значит наполненность: расчёт мог упасть на
    # середине и записать последний день по одной кампании из десяти.
    # Без проверки ширины дня гейт зеленел бы по одной строке — мутация
    # «считать широкими все дни» этого теста не переживает.
    facts = {}
    for i in range(10):
        cid = str(200 + i)
        # день начала: витрина полна по TODAY-5 включительно, дальше обрыв
        facts[cid] = _facts(cid, TODAY - timedelta(days=8), 4, cost=100.0, leads=1)
    # вчерашний день есть, но только у одной кампании
    facts["299"] = _facts("299", TODAY - timedelta(days=1), 1, cost=100.0, leads=1)
    gate = watchdog.facts_gate(_mart(facts), TODAY, max_age_days=2)

    assert gate["status"] == "RED", gate
    # Одиночная свежая строка видна отдельным полем — это улика частичного
    # отказа расчёта, а не повод считать витрину свежей.
    assert gate["latest_any_fact_date"] == (TODAY - timedelta(days=1)).isoformat()
    assert gate["latest_fact_date"] != gate["latest_any_fact_date"]


def test_gate_is_green_when_campaigns_come_and_go_within_the_window():
    # Сигнатура дефекта: кампании включаются и выключаются, в каждый день
    # активна лишь часть окна. Витрина при этом живая — гейт обязан быть
    # зелёным, иначе откат запрещён навсегда.
    facts = {}
    for i in range(6):
        cid = str(100 + i)
        # каждая кампания «работает» свои три дня подряд внутри окна
        facts[cid] = _facts(cid, TODAY - timedelta(days=3 + i), 3,
                            cost=100.0, leads=1)
    gate = watchdog.facts_gate(_mart(facts), TODAY)

    assert gate["status"] == "GREEN", gate["reason"]
    # Порог считается от типичного дня, а не от объединения за окно.
    assert gate["campaigns_required_per_day"] <= gate["campaigns_typical_per_day"]
    assert gate["campaigns_required_per_day"] < gate["campaigns_in_mart"]


def test_gate_stays_green_when_a_single_campaign_stops_delivering():
    # Обратная половина, и она же — граница: требовать свежести от КАЖДОЙ
    # кампании нельзя. У кампании, которую правка агента придушила до нуля,
    # свежих строк нет по построению, и красный гейт запретил бы откат ровно
    # там, где он нужен.
    facts = {"111": _facts("111", TODAY - timedelta(days=2), 2, cost=100.0, leads=1),
             "222": _facts("222", TODAY - timedelta(days=2), 2, cost=100.0, leads=1),
             "333": _facts("333", date(2026, 7, 20), 3, cost=100.0, leads=1)}

    assert watchdog.facts_gate(_mart(facts), TODAY)["status"] == "GREEN"


# =========================================================================
# Лаг CRM: граница окна берётся из данных, а не из константы
# =========================================================================
#
# Замер 21.08.2026: CRM EDU отстаёт на 2-4 дня, и дни приходят ЦЕЛИКОМ —
# дозревания нет (сравнение снимка edu_agent_facts с текущей CRM: leads_added=0
# на всех 45 днях). Значит опасность не в неполных днях, а в днях, которых в
# CRM ещё нет вовсе: расход Директа приехал, лидов ноль. 19.08.2026 в витрине
# лежало 927 945 рублей расхода и НОЛЬ лидов — CPA такого дня бесконечен.
#
# Прежняя защита (фиксированный отступ LEADS_LAG_DAYS=2 плюс день применения)
# переживает лаг в два-три дня и ломается на четырёх — то есть ровно тогда,
# когда нужна. Константу заменяет факт: граница из данных двигается вместе с
# ними.

def test_window_stops_at_crm_maturity_not_at_a_fixed_offset():
    # CRM отстала на четыре дня. Фиксированный отступ в три дня оставил бы в
    # окне день, где расход есть, а лидов ещё нет.
    crm_through = TODAY - timedelta(days=4)
    _, end, _ = watchdog.observation_window(_action(), TODAY, crm_through)

    assert end == crm_through
    assert end < TODAY - timedelta(days=1 + watchdog.LEADS_LAG_DAYS)


def test_window_extends_when_crm_catches_up():
    # Отставание плавает. Приехала CRM за вчера — окно обязано вырасти само,
    # иначе агент вечно судит по позавчерашним данным и реагирует медленнее,
    # чем мог бы.
    _, end, _ = watchdog.observation_window(_action(), TODAY, TODAY - timedelta(days=1))

    assert end == TODAY - timedelta(days=1)


def test_today_is_never_observed_even_if_crm_says_so():
    # Даже если CRM отдала сегодняшний день, расход за сегодня неполон:
    # наблюдаемый CPA был бы занижен, а это пропущенный вред, а не ложный.
    _, end, _ = watchdog.observation_window(_action(), TODAY, TODAY)

    assert end == TODAY - timedelta(days=1)


def test_no_crm_data_means_no_observation():
    # Лидов нет вовсе. Подставить сюда сегодня значило бы вынести вердикт по
    # пустоте: расход есть, лидов ноль, красная линия пробита на ровном месте.
    assert watchdog.observation_window(_action(), TODAY, None) is None


def test_cost_without_leads_day_never_reaches_the_verdict():
    """Сквозная проверка: день с расходом и нулём лидов не попадает в вердикт.

    Ровно эта пара чисел лежала в витрине 21.08.2026. Считай мы по ней — CPA
    улетает, красная линия пробита, здоровое изменение откатывается.
    """
    rows = _facts("111", date(2026, 8, 2), 6, cost=100.0, leads=5)
    # два дня «расход есть, лидов нет» — CRM за них ещё не приехала
    for offset in (0, 1):
        rows.append({"campaign_id": "111",
                     "fact_date": TODAY - timedelta(days=2 - offset),
                     "cost": 927945.0, "eff_leads": 0})
    facts = {"111": rows}

    report, client, _ = _run(_action(), facts,
                             crm_through=TODAY - timedelta(days=3))

    assert report["rolled_back"] == 0, "откат по дням, которых в CRM ещё нет"
    assert client.calls == []


def test_report_shows_how_far_crm_lags(monkeypatch, capsys):
    # Отставание обязано быть видно глазами: иначе «агент почему-то ничего не
    # делает» и «CRM встала неделю назад» выглядят одинаково.
    import json as _json

    # main() берёт сегодняшний день сам (date.today()), поэтому отставание
    # считается от реальной даты, а не от TODAY фикстур.
    real_today = date.today()
    _patch_watchdog_main(monkeypatch, _RecordingLock())
    monkeypatch.setattr(watchdog.agent_db, "crm_maturity_date",
                        lambda: real_today - timedelta(days=4))

    watchdog.main()

    report = _json.loads(capsys.readouterr().out)
    assert report["crm_through"] == (real_today - timedelta(days=4)).isoformat()
    assert report["crm_lag_days"] == 4


# ------------------------- объёмная красная линия: обвал расхода


def test_spend_collapse_is_breach():
    # Слепое пятно CPA-линии: вредная правка, придушившая кампанию, не
    # набирает ни расхода, ни лидов — CPA-порог молчит, min_leads не
    # достигается никогда, изменение живёт «под наблюдением» до истечения
    # горизонта. Обвал расхода ниже 30 % от ожидаемого дневного (risk_rub —
    # это и есть ожидаемый расход за горизонт замера) — сам по себе пробой.
    action = _action(risk_rub=7000.0)  # ожидание 1000 ₽/день
    observed = {"cost": 600.0, "leads": 0, "cpa": 0.0, "days": 3}  # 200 ₽/день
    collapsed, reason = is_spend_collapsed(action, observed)
    assert collapsed
    assert "обвал" in reason


def test_spend_collapse_needs_minimum_days():
    action = _action(risk_rub=7000.0)
    observed = {"cost": 200.0, "leads": 0, "cpa": 0.0, "days": 2}
    collapsed, _ = is_spend_collapsed(action, observed)
    assert not collapsed


def test_spend_collapse_ignores_intentional_suspend():
    # campaign.suspend глушит кампанию НАМЕРЕННО — нулевой расход после него
    # это исполнение решения, а не деградация.
    action = _action(action_kind="campaign.suspend", risk_rub=7000.0)
    observed = {"cost": 0.0, "leads": 0, "cpa": 0.0, "days": 5}
    collapsed, _ = is_spend_collapsed(action, observed)
    assert not collapsed


def test_spend_collapse_without_risk_baseline_is_silent():
    # risk_rub нет или ноль — ожидаемого расхода нет, доли не от чего считать.
    action = _action(risk_rub=0.0)
    observed = {"cost": 0.0, "leads": 0, "cpa": 0.0, "days": 5}
    collapsed, _ = is_spend_collapsed(action, observed)
    assert not collapsed


def test_healthy_spend_does_not_collapse():
    action = _action(risk_rub=7000.0)
    observed = {"cost": 2700.0, "leads": 1, "cpa": 2700.0, "days": 3}
    collapsed, _ = is_spend_collapsed(action, observed)
    assert not collapsed


def test_judge_turns_spend_collapse_into_breach():
    # Вайринг: judge выносит BREACHED по обвалу расхода ДО проверки
    # min_leads — иначе обвал (мало лидов по построению) навсегда застревал
    # бы в «наблюдений N из 20».
    action = _action(risk_rub=7000.0)
    facts = {"111": _facts("111", date(2026, 8, 2), 4, 100.0, 0)}
    verdict = watchdog.judge(action, facts, date(2026, 8, 6), _mart(facts),
                             date(2026, 8, 5))
    assert verdict["state"] == watchdog.STATE_BREACHED
    assert "обвал" in verdict["reason"]


# ------------------- G: глобальная уборка зависших и --fail-on-alarm


def test_boevoy_watchdog_sweeps_stale_globally(monkeypatch, capsys):
    # Помечать зависшие строки умел только e1 — и только по СВОЕМУ логину.
    # Кабинет, по которому e1 больше не запускают, копил planned-строки
    # вечно. Боевой сторож (prod+apply) метёт ВЕСЬ журнал, без фильтра по
    # аккаунту, до чтения open_actions — свежепомеченные строки попадают
    # под наблюдение этим же прогоном.
    calls = []
    lock = _RecordingLock()
    _patch_watchdog_main(monkeypatch, lock)
    monkeypatch.setattr(sys, "argv", ["agent_e1_watchdog", "--prod", "--apply"])
    monkeypatch.setattr(watchdog.writer_db, "mark_stale_planned",
                        lambda minutes, account=None: calls.append(
                            (minutes, account)) or [])

    assert watchdog.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert calls == [(watchdog.STALE_SWEEP_MINUTES, None)]
    assert out["stale_marked"] == {"count": 0, "sample": []}


def test_rehearsal_watchdog_does_not_touch_the_journal(monkeypatch, capsys):
    lock = _RecordingLock()
    _patch_watchdog_main(monkeypatch, lock)  # argv без --apply: репетиция
    monkeypatch.setattr(watchdog.writer_db, "mark_stale_planned",
                        lambda *a, **k: pytest.fail(
                            "репетиция не пишет в журнал"))
    assert watchdog.main() == 0
    capsys.readouterr()


def test_alarm_reasons_cover_human_needed_states():
    quiet = {"needs_manual_rollback": 0, "data_gate": {"status": "GREEN"},
             "stale_marked": {"count": 0, "sample": []},
             "accounts": [{"account": "a", "rolled_back": 0,
                           "rollback_failed": 0, "errors": [],
                           "needs_review": 0}]}
    assert watchdog.alarm_reasons(quiet) == []

    loud = {"needs_manual_rollback": 2, "data_gate": {"status": "RED"},
            "stale_marked": {"count": 1, "sample": []},
            "accounts": [{"account": "a", "rolled_back": 1,
                          "rollback_failed": 1,
                          "errors": [{"error": "x"}], "needs_review": 3}]}
    reasons = watchdog.alarm_reasons(loud)
    assert len(reasons) >= 5


def test_fail_on_alarm_flag_turns_alarms_into_nonzero_exit(monkeypatch, capsys):
    # Крон-письмо GitHub приходит только на КРАСНЫЙ ран: тревога без
    # ненулевого выхода — молчание. Флаг явный: локальный запуск руками не
    # должен становиться красным из-за старой накопительной находки.
    lock = _RecordingLock()
    _patch_watchdog_main(monkeypatch, lock)
    monkeypatch.setattr(sys, "argv",
                        ["agent_e1_watchdog", "--prod", "--fail-on-alarm"])
    monkeypatch.setattr(watchdog.writer_db, "failed_rollbacks_count", lambda: 2)

    assert watchdog.main() == 1
    out = json.loads(capsys.readouterr().out)
    assert out["alarms"]

    _patch_watchdog_main(monkeypatch, lock)  # без флага — тот же прогон зелёный
    monkeypatch.setattr(watchdog.writer_db, "failed_rollbacks_count", lambda: 2)
    assert watchdog.main() == 0
    capsys.readouterr()


# ------------------- C4: исход по непрерывному эффекту, а не «не пробил»


def test_outcome_verdict_calls_moderate_harm_harm():
    from sync.agent.writer.rollback import outcome_verdict
    # CPA на 25 % выше базы аварийный порог (+40 %) не пробивает, но это
    # ПРОВАЛ, а не «выдержало»: в обучающую историю он обязан лечь как вред.
    assert outcome_verdict(0.25, rel_error=0.05) == "worsened"
    assert outcome_verdict(-0.20, rel_error=0.05) == "improved"


def test_outcome_verdict_is_inconclusive_within_one_sigma():
    from sync.agent.writer.rollback import outcome_verdict
    # Эффект меньше собственной ошибки — исход неотличим от нуля. «Улучшение»
    # здесь было бы winner's curse: действия отбираются по экстремальным
    # оценкам, и шум систематически читается как успех.
    assert outcome_verdict(0.05, rel_error=0.20) == "inconclusive"
    assert outcome_verdict(None, rel_error=0.20) == "unknown"


def test_closed_action_experiment_carries_graded_verdict():
    action = _action()
    window = (date(2026, 8, 2), date(2026, 8, 15), True)
    observed = {"cost": 12500.0, "leads": 100, "cpa": 892.5, "days": 14}
    exp = watchdog.action_experiment(action, observed, window,
                                     watchdog.closing_verdict(observed, action))
    # База 714, наблюдаемая 892.5 → +25 % при ошибке 0.1: вред, не «held».
    assert exp["verdict"] == "worsened"
    assert exp["effect"] > 0


# ------------------- сезонный контроль красной линии


def _totals(start, days, cost, leads):
    return [{"fact_date": start + timedelta(days=i), "cost": cost,
             "eff_leads": leads} for i in range(days)]


def test_seasonal_factor_follows_the_control_and_is_capped():
    # Кабинет в целом подорожал вдвое — это сезон, а не вред конкретной
    # правки. Порог обязан подвинуться следом, но не безгранично: контроль
    # сам дёргается, и без зажима «сезоном» оправдывался бы любой рост.
    base = _totals(date(2026, 7, 1), 30, 1000.0, 20)      # CPA 50
    window = _totals(date(2026, 8, 2), 14, 1000.0, 10)    # CPA 100
    factor = watchdog.seasonal_factor(
        base + window, (date(2026, 7, 1), date(2026, 7, 30)),
        (date(2026, 8, 2), date(2026, 8, 15)))
    assert factor == watchdog.SEASONAL_CAP


def test_seasonal_factor_is_one_without_control_data():
    assert watchdog.seasonal_factor([], (date(2026, 7, 1), date(2026, 7, 30)),
                                    (date(2026, 8, 2), date(2026, 8, 15))) == 1.0


def test_season_wide_cpa_rise_does_not_breach_the_red_line():
    # У кампании CPA вырос на 40 % — ровно как у всего кабинета. Это перелом
    # сезона, а не вредная правка: массовые откаты здоровых изменений на
    # смене сезона — как раз то, чего красная линия без контроля не различает.
    action = _action(red_line={"metric": "cpa", "max_value": 1000.0,
                               "min_leads": 20, "baseline_cpa": 714.0,
                               "has_baseline": True,
                               "baseline_from": "2026-07-01",
                               "baseline_to": "2026-07-30"})
    facts = {"111": _facts("111", date(2026, 8, 2), 14, 1000.0, 1)}  # CPA 1000
    totals = (_totals(date(2026, 7, 1), 30, 1000.0, 20)              # CPA 50
              + _totals(date(2026, 8, 2), 14, 1400.0, 20))           # CPA 70 (+40%)
    verdict = watchdog.judge(action, facts, date(2026, 8, 20), _mart(facts),
                             date(2026, 8, 19), account_totals=totals)
    assert verdict["state"] != watchdog.STATE_BREACHED
    assert verdict["seasonal_factor"] > 1.0


# ------------------- досрочное закрытие: не ждать горизонта без нужды


def test_watch_closes_early_when_the_verdict_is_already_certain():
    # Кампания набрала объём и показала определённый эффект на первой неделе.
    # Ждать полного горизонта незачем: пока действие «под наблюдением», оно
    # держит риск-бюджет и кулдаун, и следующая правка этой кампании не
    # начинается. На живых данных 39 % кампаний набирают порог за 7 дней —
    # для них горизонт вдвое длиннее необходимого.
    action = _action()
    # today выбран так, чтобы окно наблюдения было ровно теми 7 днями, за
    # которые есть факты: иначе прогон честно упрётся в неполное покрытие.
    facts = {"111": _facts("111", date(2026, 8, 2), 7, cost=1000.0, leads=10)}
    report, client, db = _run(action, facts, today=date(2026, 8, 11))
    assert report["closed_held"] == 1
    assert db.observation_closed == [("act-1", "improved", None)]


def test_early_close_needs_a_full_week():
    # Три дня — не наблюдение: недельный ритм рекламы (будни против выходных)
    # сам по себе даёт разницу, которую легко принять за эффект.
    action = _action()
    facts = {"111": _facts("111", date(2026, 8, 2), 3, cost=1000.0, leads=20)}
    report, client, db = _run(action, facts, today=date(2026, 8, 9))
    assert report["closed_held"] == 0


def test_early_close_does_not_fire_on_an_inconclusive_effect():
    # Эффект меньше собственной ошибки — исход неотличим от нуля. Такое
    # действие обязано досматриваться до конца горизонта: вдруг определится.
    action = _action(red_line={"metric": "cpa", "max_value": 1000.0,
                               "min_leads": 20, "baseline_cpa": 700.0,
                               "has_baseline": True})
    facts = {"111": _facts("111", date(2026, 8, 2), 8, cost=2800.0, leads=4)}
    report, client, db = _run(action, facts, today=date(2026, 8, 14))
    assert report["closed_held"] == 0


def test_observed_leads_delta_is_measured_against_base_rate():
    """Наблюдаемая дельта = лиды окна − темп базы × длина окна.

    Не разность сумм: окно базы 28 дней, окно наблюдения 7–14, и голая
    разность сумм показала бы обвал там, где темп вырос.
    """
    action = {"red_line": {"baseline_leads_per_day": 2.0}}
    observed = {"leads": 21, "days": 7}          # темп 3.0 против базовых 2.0

    assert watchdog.observed_leads_delta(observed, action) == 7.0


def test_observed_leads_delta_is_none_without_base_rate():
    # Действие спланировано до появления темпа базы в линии — сравнивать не
    # с чем. None, а не ноль: ноль петля обучения прочитала бы как «эффекта
    # ровно не было», то есть как измерение, которого не делали.
    assert watchdog.observed_leads_delta({"leads": 21, "days": 7},
                                         {"red_line": {}}) is None
    assert watchdog.observed_leads_delta({"leads": 0, "days": 0},
                                         {"red_line": {"baseline_leads_per_day": 2.0}}) is None


def test_closing_writes_the_observed_leads_delta_to_the_journal():
    # Пара «ожидание / факт» замыкается здесь: темп базы лежал в линии с
    # момента планирования, лиды окна знает сторож — журнал получает разницу.
    action = _action(red_line={**_action()["red_line"],
                               "baseline_leads_per_day": 1.0})
    report, client, db = _run(action, _held_facts(), today=date(2026, 9, 1))

    observed = report["closed_held_sample"][0]["observed"]
    expected = round(observed["leads"] - 1.0 * observed["days"], 2)
    assert expected > 0
    assert db.observation_closed == [("act-1", "improved", expected)]


# =========================================================================
# Задача 13: петля обучения — класс A за контроль и экономический исход
# =========================================================================


def _obs_window():
    return (date(2026, 8, 2), date(2026, 8, 15), True)


def test_class_a_is_earned_by_control_not_by_authorship():
    """Знание даты не отменяет сезон: класс A даёт контроль, а не авторство."""
    action = _action()                       # база 714
    observed = {"cpa": 892.5, "leads": 40}

    without = watchdog.action_experiment(action, observed, _obs_window(), "worsened")
    assert without["reliability_class"] == "B"
    assert without["mechanism"] == "before_after"
    assert without["effect"] == 0.25

    # Заповедник подорожал ровно так же — значит это сезон, а не действие.
    control = {"baseline_cpa": 714.0, "cpa": 892.5, "leads": 50}
    with_control = watchdog.action_experiment(action, observed, _obs_window(),
                                              "worsened", control=control)
    assert with_control["reliability_class"] == "A"
    assert with_control["mechanism"] == "did_holdout"
    assert with_control["effect"] == 0.0
    assert with_control["params"]["control"]["leads"] == 50


def test_thin_control_does_not_upgrade_class():
    # Контроль на трёх лидах — шум, а не эталон: завышенный класс надёжности
    # дороже отсутствующего, на классах строится автономия.
    out = watchdog.action_experiment(
        _action(), {"cpa": 892.5, "leads": 40}, _obs_window(), "worsened",
        control={"baseline_cpa": 714.0, "cpa": 892.5, "leads": 3})
    assert out["reliability_class"] == "B"
    assert out["mechanism"] == "before_after"


def test_control_without_a_baseline_price_does_not_upgrade_class():
    out = watchdog.action_experiment(
        _action(), {"cpa": 892.5, "leads": 40}, _obs_window(), "worsened",
        control={"baseline_cpa": 0.0, "cpa": 892.5, "leads": 50})
    assert out["reliability_class"] == "B"


def test_holdout_control_measures_the_same_two_windows():
    rows = (_facts("900", date(2026, 7, 1), 10, cost=1000.0, leads=2)
            + _facts("900", date(2026, 8, 2), 14, cost=1200.0, leads=2))
    control = watchdog.holdout_control(rows, (date(2026, 7, 1), date(2026, 7, 10)),
                                       (date(2026, 8, 2), date(2026, 8, 15)))
    assert control == {"baseline_cpa": 500.0, "cpa": 600.0, "leads": 28}


def test_holdout_control_is_none_without_a_reserve_or_a_window():
    windows = ((date(2026, 7, 1), date(2026, 7, 10)),
               (date(2026, 8, 2), date(2026, 8, 15)))
    assert watchdog.holdout_control([], *windows) is None
    assert watchdog.holdout_control(
        _facts("900", date(2026, 8, 2), 14, cost=1200.0, leads=2), None,
        windows[1]) is None
    # Заповедник без лидов в базовом окне: делить не на что.
    assert watchdog.holdout_control(
        _facts("900", date(2026, 8, 2), 14, cost=1200.0, leads=2), *windows) is None


# ------------------- экономический исход растящего действия


def _growth_action(expected=12.0, kind="budget.set"):
    return _action(action_kind=kind,
                   payload={"expected_leads_delta": expected},
                   red_line={**_action()["red_line"],
                             "baseline_leads_per_day": 5.0})


def test_growth_is_judged_by_volume_at_an_acceptable_price():
    # Доливка обещала ОБЪЁМ: лиды выросли, цена осталась под потолком линии.
    # Мера по цене назвала бы это провалом — за объём и платят дороже.
    action = _growth_action()
    observed = {"cpa": 892.5, "leads": 100, "days": 14}

    assert watchdog.closing_verdict(observed, action) == "worsened"
    assert watchdog.economic_outcome(action, observed, 1000.0) == "improved"


def test_growth_that_did_not_buy_volume_is_a_miss_even_if_it_got_cheaper():
    action = _growth_action()
    observed = {"cpa": 600.0, "leads": 60, "days": 14}   # темп базы 5/д → −10

    assert watchdog.closing_verdict(observed, action) == "improved"
    assert watchdog.economic_outcome(action, observed, 1000.0) == "worsened"


def test_growth_above_the_ceiling_is_not_a_hit():
    action = _growth_action()
    observed = {"cpa": 1200.0, "leads": 100, "days": 14}
    assert watchdog.economic_outcome(action, observed, 1000.0) == "inconclusive"


def test_growth_without_a_measured_fact_stays_unknown():
    action = _action(action_kind="budget.set",
                     payload={"expected_leads_delta": 12.0})   # темпа базы нет
    assert watchdog.economic_outcome(
        action, {"cpa": 892.5, "leads": 100, "days": 14}, 1000.0) == "unknown"


def test_cuts_and_other_levers_keep_the_price_measure():
    cut = _growth_action(expected=-8.0)
    observed = {"cpa": 600.0, "leads": 60, "days": 14}
    assert watchdog.economic_outcome(cut, observed, 1000.0) == "improved"

    # Рычаг вне списка растящих судится ценой, даже если ожидание положительное.
    other = _growth_action(kind="bidmodifier.set")
    assert (watchdog.economic_outcome(other, {"cpa": 892.5, "leads": 100, "days": 14},
                                      1000.0) == "worsened")


def test_closing_stores_the_economic_outcome_for_a_growth_action():
    # Сквозная проверка: в журнал уходит исход по обещанию действия, а не по
    # цене. Тот же прогон по прежней мере записал бы «worsened».
    action = _growth_action(expected=10.0)
    action["red_line"] = {**action["red_line"], "baseline_leads_per_day": 1.0}
    facts = {"111": _facts("111", date(2026, 8, 2), 14, cost=1800.0, leads=2)}
    report, client, db = _run(action, facts, today=date(2026, 9, 1))

    observed = report["closed_held_sample"][0]["observed"]
    assert observed["cpa"] == 900.0
    assert watchdog.closing_verdict(observed, action) == "worsened"
    assert db.observation_closed == [("act-1", "improved", 14.0)]
    assert db.experiments[0]["verdict"] == "improved"


def test_closing_upgrades_the_class_when_the_reserve_covers_the_window():
    action = _action(red_line={**_action()["red_line"],
                               "baseline_from": "2026-07-01",
                               "baseline_to": "2026-07-30"})
    facts = {"111": _facts("111", date(2026, 8, 2), 14, cost=700.0, leads=2)}
    holdout_facts = (_facts("900", date(2026, 7, 1), 30, cost=1000.0, leads=2)
                     + _facts("900", date(2026, 8, 2), 14, cost=1000.0, leads=2))
    db = _FakeDb()
    watchdog.watch(_FakeClient(), [action], db, {"900"}, facts,
                   date(2026, 9, 1), GREEN_GATE, date(2026, 8, 29), None,
                   _mart(facts), None, holdout_facts)

    assert db.experiments[0]["mechanism"] == "did_holdout"
    assert db.experiments[0]["reliability_class"] == "A"
