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


# ── Сводка недельного разбора ────────────────────────────────────────────────
#
# Главная проверка здесь не форматирование, а правило sync/agent/value.py:
# НЕИЗМЕРЕННОЕ НЕ РАВНО НУЛЮ. Сводка, печатающая «0 ₽» там, где замер сказал
# «утверждать нечего», врёт владельцу ровно в ту сторону, в которую он и
# боится ошибиться.

GREEN = {"findings": [], "by_severity": {"high": 0, "medium": 0, "low": 0}}


def test_review_green_says_so():
    text = notify.review_summary(GREEN)
    assert "Разбор недели" in text
    assert "Проблем разбор не нашёл." in text


def test_review_unmeasured_is_not_zero_rubles():
    value = {"saved_rub": 0.0, "earned_rub": 0.0, "cut_rub": 0.0,
             "n_tacts": 3, "n_tacts_measured": 0,
             "n_actions": 40, "n_actions_measured": 0,
             "unmeasured_share": 1.0, "did_interval_rub": None}
    text = notify.review_summary(GREEN, value)
    assert "не измерено ни одного наблюдения" in text
    assert "0 ₽" not in text


def test_review_money_carries_interval_and_unmeasured_share():
    value = {"saved_rub": 42400.0, "did_interval_rub": [8000.0, 90000.0],
             "earned_rub": 12000.0, "cut_rub": -3000.0,
             "n_tacts": 2, "n_tacts_measured": 1,
             "n_actions": 30, "n_actions_measured": 9,
             "unmeasured_share": 0.6875}
    text = notify.review_summary(GREEN, value)
    assert "сэкономил" in text and "42" in text
    # Интервал обязан быть виден: одна сумма без него читается как точное знание.
    assert "от" in text and "до" in text
    assert "69 %" in text and "не ноль" in text


def test_review_trend_against_previous_week():
    now = {"applied": 12, "risk_rub": 40000, "pending_approval": 2,
           "rejected": 1, "rolled_back": 0}
    prev = {"applied": 20, "risk_rub": 10000}
    text = notify.review_summary(GREEN, None, now, prev)
    assert "Применено: 12" in text
    assert "-8 к прошлой неделе" in text
    assert "ждут твоего слова 2" in text and "отклонено тобой 1" in text
    # Ноль откатов не печатается: строка «откачено 0» шумит, а не сообщает.
    assert "откачено" not in text


def test_review_same_as_last_week_is_said_in_words():
    text = notify.review_summary(GREEN, None, {"applied": 7}, {"applied": 7})
    assert "столько же" in text


def test_review_shows_top_findings_and_counts_the_rest():
    findings = [{"code": f"C{i}", "severity": "high" if i == 0 else "medium",
                 "subject": f"кампания {i}", "detail": "стена"} for i in range(6)]
    text = notify.review_summary({"findings": findings}, None)
    assert "Проблем: 6, из них срочных 1." in text
    assert "СРОЧНО: кампания 0 — стена" in text
    assert "и ещё 3" in text


def test_review_drops_low_severity():
    # Вес low живёт в логе прогона: сводка, где перечислено всё, читается как
    # «ничего важного» ровно так же, как пустая.
    findings = [{"code": "C", "severity": "low", "subject": "s", "detail": "d"}]
    text = notify.review_summary({"findings": findings})
    assert "Проблем разбор не нашёл." in text


def test_e1_summary_names_the_price_of_approval():
    report = {"accounts": [{"result": {"applied": 3}}],
              "pending_approvals": [{"risk_rub": 7800.0}, {"risk_rub": 7600.0}]}
    text = notify.e1_summary(report, dry_run=False)
    assert "Прошу апрув: 2" in text
    assert "15" in text and "₽ риска" in text


def test_e1_summary_without_risk_stays_readable():
    report = {"accounts": [{"result": {"applied": 1}}],
              "pending_approvals": [{}]}
    text = notify.e1_summary(report, dry_run=False)
    assert "Прошу апрув: 1" in text and "риска" not in text


def test_review_interval_crossing_zero_is_not_called_saving():
    # Интервал по обе стороны нуля — «утверждать нечего». Слово «сэкономил»
    # рядом с ним присвоило бы агенту деньги, которых замер ему не отдал.
    value = {"saved_rub": 42400.0, "did_interval_rub": [-5000.0, 90000.0],
             "earned_rub": 0.0, "cut_rub": 0.0,
             "n_tacts": 2, "n_tacts_measured": 1,
             "n_actions": 0, "n_actions_measured": 0, "unmeasured_share": 0.5}
    text = notify.review_summary(GREEN, value)
    assert "сэкономил" not in text
    assert "не отличима от нуля" in text


def test_review_interval_fully_positive_is_a_saving():
    value = {"saved_rub": 42400.0, "did_interval_rub": [8000.0, 90000.0],
             "earned_rub": 0.0, "cut_rub": 0.0,
             "n_tacts": 2, "n_tacts_measured": 2,
             "n_actions": 0, "n_actions_measured": 0, "unmeasured_share": 0.0}
    text = notify.review_summary(GREEN, value)
    assert "сэкономил" in text
