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


def test_e1_summary_names_applied_and_rejected():
    report = {"verdict": "GREEN", "accounts": [{"account": "acc", "planned": 240,
               "result": {"applied": 3, "failed": 0, "unknown_outcome": 0, "dry_run": 0},
               "rejects": {"lane_limit": 174, "budget": 6},
               "lanes": {"taken": {"tuning": 20, "hygiene": 10}}}]}
    text = notify.e1_summary(report, dry_run=False)
    assert "acc" in text and "применено 3" in text and "lane_limit 174" in text
    assert "БОЕВАЯ" in text


def test_e1_summary_in_rehearsal_counts_dry_run_not_applied():
    """В репетиции ничего не применено — счёт «применённого» идёт по dry_run."""
    report = {"verdict": "GREEN", "accounts": [{"account": "acc", "planned": 100,
               "result": {"applied": 0, "failed": 0, "unknown_outcome": 0, "dry_run": 42},
               "rejects": {}, "lanes": {"taken": {}}}]}
    text = notify.e1_summary(report, dry_run=True)
    assert "применено 42" in text
    assert "репетиция" in text
    assert "БОЕВАЯ" not in text


def test_e1_summary_reports_zero_applied_silence_is_a_signal():
    report = {"verdict": "NOTHING_TO_DO", "accounts": [{"account": "acc", "planned": 0,
               "result": {"applied": 0, "failed": 0, "unknown_outcome": 0, "dry_run": 0},
               "rejects": {}, "lanes": {"taken": {}}}]}
    text = notify.e1_summary(report, dry_run=False)
    assert "применено 0" in text


def test_watchdog_summary_names_account_alarm_and_rollbacks():
    out = {"verdict": "ALARM",
           "alarms": ["Кабинет acc: пробита красная линия по CPA на 3 объектах"],
           "under_watch": 12,
           "needs_manual_rollback": 1,
           "accounts": [{"account": "acc", "rolled_back": 2, "breached": 3,
                         "rollback_failed": 1,
                         "closed_verdicts": {"held": 4, "harmed": 1},
                         "needs_review": 2}]}
    text = notify.watchdog_summary(out)
    assert "acc" in text
    assert "пробита красная линия по CPA на 3 объектах" in text
    assert "откатов 2" in text


def test_watchdog_summary_green_has_no_alarms_block():
    out = {"verdict": "GREEN", "alarms": [], "under_watch": 5,
           "needs_manual_rollback": 0,
           "accounts": [{"account": "acc", "rolled_back": 0, "breached": 0,
                         "rollback_failed": 0, "closed_verdicts": {}, "needs_review": 0}]}
    text = notify.watchdog_summary(out)
    assert "ТРЕВОГИ" not in text
    assert "откатов 0" in text
