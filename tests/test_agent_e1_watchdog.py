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

    def __init__(self, dry_run=False, response=None, raises=None):
        self.dry_run = dry_run
        self.response = response if response is not None else {"SetResults": [{"Id": 7}]}
        self.raises = raises
        self.calls = []
        self.units_left = None

    def is_write_allowed(self):
        return not self.dry_run

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


def _run(action, facts, client=None, db=None, holdout=(), today=TODAY):
    client = client or _FakeClient()
    db = db or _FakeDb()
    report = watchdog.watch(client, [action], db, {str(h) for h in holdout},
                            facts, today)
    return report, client, db


# --------------------------------------------------------------- окно наблюдения

def test_window_starts_after_application_day_and_ends_yesterday():
    start, end, closed = watchdog.observation_window(_action(), TODAY)
    assert start == date(2026, 8, 2)   # день применения не наблюдаем
    assert end == date(2026, 8, 9)     # сегодняшние факты неполны
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


def test_guardrails_see_rollback_coefficient_in_delta_scale():
    # Нейтраль отката в шкале API — 100. Прочитанная рельсой как «+100 %», она
    # отклонялась бы потолком ±50, и штатный откат не проходил бы никогда.
    form = watchdog.guard_form(_action(), "bidmodifiers", "set",
                               {"BidModifiers": [{"Id": 7, "BidModifier": 100}]})
    assert form["payload"]["BidModifier"] == 0
    assert form["action_kind"] == "bidmodifier.set"


def test_guardrails_reject_rollback_beyond_modifier_cap():
    action = _action(previous_state={"Id": 7, "percent": 170})
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

    report = watchdog.watch(client, [_action(), healthy], db, set(), facts, TODAY)

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

    report = watchdog.watch(client, [broken, _action()], db, set(), facts, TODAY)

    assert report["errors"] == 1
    assert report["errors_sample"][0]["object_id"] == "333"
    assert report["rolled_back"] == 1          # здоровое действие рассужено
    assert db.rolled_back == ["act-1"]


def test_facts_window_covers_every_action_window():
    old = _action(action_id="old", applied_at=datetime(2026, 7, 20, 10, 0))
    span = watchdog.facts_window([old, _action()], TODAY)
    assert span == (date(2026, 7, 21), date(2026, 8, 9))


def test_facts_window_is_none_when_no_action_is_observable_yet():
    assert watchdog.facts_window([_action()], date(2026, 8, 2)) is None


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
