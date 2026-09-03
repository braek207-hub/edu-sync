# -*- coding: utf-8 -*-
"""sync/agent/notify.py — уведомления человеку в Telegram.

Ключи в тестах — реальные (сняты с прода edu_agent_runs 29.08.2026), а не
из черновика задачи: у report.accounts[i].result нет "stale" (это
applied/failed/unknown_outcome/dry_run), а у watchdog rolled_back/breached/
closed_verdicts/rollback_failed/needs_review живут ПО АККАУНТАМ, не сверху
(там только alarms/under_watch/needs_manual_rollback).
"""
from sync.agent import notify


def test_not_configured_is_silent(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert notify.send("x") == {"sent": False, "reason": "not_configured"}


def test_transport_error_never_raises(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setattr(notify, "_post",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("net")))
    out = notify.send("x")
    assert out["sent"] is False and "OSError" in out["reason"]


def test_transport_error_masks_token_in_reason(monkeypatch):
    # http.client.InvalidURL и подобные исключения могут нести URL целиком —
    # а в URL зашит токен бота. Токен не должен утечь в NOTIFY-строку лога.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")

    def _boom(*a, **k):
        raise ValueError("bad url /botsecret123/sendMessage")

    monkeypatch.setattr(notify, "_post", _boom)
    out = notify.send("x")
    assert out["sent"] is False
    assert "secret123" not in out["reason"]
    assert "***" in out["reason"]


def test_send_success_reports_sent(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    calls = []
    monkeypatch.setattr(notify, "_post", lambda url, data: calls.append((url, data)))
    out = notify.send("привет")
    assert out == {"sent": True, "reason": None}
    assert len(calls) == 1
    url, data = calls[0]
    assert url == "https://api.telegram.org/bott/sendMessage"
    assert b"\xd0\xbf\xd1\x80\xd0\xb8\xd0\xb2\xd0\xb5\xd1\x82" in data  # "привет" utf-8


def test_e1_summary_sums_accounts_and_hides_reject_codes():
    """Формат для человека (решение 03.09): суммы по кабинетам, коды причин
    отказов в текст не попадают — их место в логе и чёрном ящике."""
    report = {"verdict": "GREEN", "accounts": [
        {"account": "acc-1", "planned": 240,
         "result": {"applied": 3, "failed": 0, "unknown_outcome": 0, "dry_run": 0},
         "rejects": {"lane_limit": 174, "budget": 6},
         "lanes": {"taken": {"tuning": 20}}},
        {"account": "acc-2", "planned": 10,
         "result": {"applied": 2, "failed": 0, "unknown_outcome": 0, "dry_run": 0},
         "rejects": {"budget": 20}, "lanes": {"taken": {}}}]}
    text = notify.e1_summary(report, dry_run=False)
    assert "Сделал сам: 5" in text
    assert "200 отложил" in text
    assert "lane_limit" not in text and "budget" not in text
    assert "всё ок" in text


def test_e1_summary_announces_pending_approvals():
    report = {"verdict": "GREEN", "pending_approvals": [{}, {}],
              "accounts": [{"account": "acc", "planned": 5,
               "result": {"applied": 1, "failed": 0, "unknown_outcome": 0, "dry_run": 0},
               "rejects": {}, "lanes": {"taken": {}}}]}
    text = notify.e1_summary(report, dry_run=False)
    assert "Прошу апрув: 2" in text


def test_e1_summary_without_pending_says_no_big_actions():
    report = {"verdict": "GREEN", "accounts": [{"account": "acc", "planned": 5,
               "result": {"applied": 1, "failed": 0, "unknown_outcome": 0, "dry_run": 0},
               "rejects": {}, "lanes": {"taken": {}}}]}
    text = notify.e1_summary(report, dry_run=False)
    assert "Крупных действий не предлагаю" in text


def test_e1_summary_surfaces_failures():
    report = {"verdict": "PARTIAL", "failed_accounts": [{"account": "acc-3"}],
              "accounts": [{"account": "acc", "planned": 5,
               "result": {"applied": 1, "failed": 2, "unknown_outcome": 1, "dry_run": 0},
               "rejects": {}, "lanes": {"taken": {}}}]}
    text = notify.e1_summary(report, dry_run=False)
    assert "ВНИМАНИЕ" in text and "сбоев 2" in text
    assert "неясный исход 1" in text and "acc-3" in text
    assert "есть проблемы" in text


def test_e1_summary_in_rehearsal_counts_dry_run_not_applied():
    """В репетиции ничего не применено — счёт «применённого» идёт по dry_run."""
    report = {"verdict": "GREEN", "accounts": [{"account": "acc", "planned": 100,
               "result": {"applied": 0, "failed": 0, "unknown_outcome": 0, "dry_run": 42},
               "rejects": {}, "lanes": {"taken": {}}}]}
    text = notify.e1_summary(report, dry_run=True)
    assert "Применилось бы: 42" in text
    assert "Сделал сам" not in text
    assert "репетиция" in text


def test_e1_summary_reports_zero_applied_silence_is_a_signal():
    report = {"verdict": "NOTHING_TO_DO", "accounts": [{"account": "acc", "planned": 0,
               "result": {"applied": 0, "failed": 0, "unknown_outcome": 0, "dry_run": 0},
               "rejects": {}, "lanes": {"taken": {}}}]}
    text = notify.e1_summary(report, dry_run=False)
    assert "Сделал сам: 0" in text


def test_watchdog_summary_names_alarm_and_rollbacks():
    out = {"verdict": "ALARM",
           "alarms": ["Кабинет acc: пробита красная линия по CPA на 3 объектах"],
           "under_watch": 12,
           "needs_manual_rollback": 1,
           "accounts": [{"account": "acc", "rolled_back": 2, "breached": 3,
                         "rollback_failed": 1,
                         "closed_verdicts": {"held": 4, "harmed": 1},
                         "needs_review": 2}]}
    text = notify.watchdog_summary(out)
    assert "ЕСТЬ ПРОБЛЕМЫ" in text
    assert "пробита красная линия по CPA на 3 объектах" in text
    assert "Откатил 2" in text
    assert "НЕ СМОГ откатить 1" in text
    assert "Нужны руки: 1" in text


def test_watchdog_summary_green_is_one_calm_line():
    out = {"verdict": "GREEN", "alarms": [], "under_watch": 5,
           "needs_manual_rollback": 0,
           "accounts": [{"account": "acc", "rolled_back": 0, "breached": 0,
                         "rollback_failed": 0, "closed_verdicts": {}, "needs_review": 0}]}
    text = notify.watchdog_summary(out)
    assert "ТРЕВОГА" not in text and "ПРОБЛЕМЫ" not in text
    assert "вредных не нашёл" in text and "На замере 5" in text


def test_e1_summary_shows_the_launch_pipeline_and_proposals():
    """Решение 03.09: невидимая половина работы агента — конвейер новых
    кампаний и идеи-предложения — видна в каждой сводке."""
    report = {"verdict": "GREEN",
              "launch_queue": {"building": 2, "built_waiting": 1},
              "proposal_open": 229,
              "accounts": [{"account": "acc", "planned": 5,
               "result": {"applied": 1, "failed": 0, "unknown_outcome": 0, "dry_run": 0},
               "rejects": {}, "lanes": {"taken": {}}}]}
    text = notify.e1_summary(report, dry_run=False)
    assert "Новые кампании: 2 в сборке, 1 собрано" in text
    assert "«да» на включение" in text
    assert "Идей-предложений в копилке: 229" in text


def test_e1_summary_is_silent_about_an_empty_launch_pipeline():
    report = {"verdict": "GREEN",
              "launch_queue": {"building": 0, "built_waiting": 0},
              "proposal_open": 0,
              "accounts": [{"account": "acc", "planned": 5,
               "result": {"applied": 1, "failed": 0, "unknown_outcome": 0, "dry_run": 0},
               "rejects": {}, "lanes": {"taken": {}}}]}
    text = notify.e1_summary(report, dry_run=False)
    assert "Новые кампании" not in text
    assert "копилке" not in text
