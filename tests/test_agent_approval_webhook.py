# -*- coding: utf-8 -*-
"""Переезд апрув-контура с getUpdates на вебхук Panda-BI.

Bot API не отдаёт getUpdates, пока у бота стоит вебхук. Вебхук теперь держит
Panda-BI (app/api/telegram/webhook): он принимает нажатия кнопок и пишет слово
человека в edu_agent_approvals, а воркер читает решения оттуда. Здесь
проверяется ровно этот стык — иначе контур молча ослепнет: очередь есть,
решения человека есть, а применения нет.
"""
import pytest

from sync import agent_approver
from sync.agent import approval, approval_db, notify


class TestDecisionSource:
    def test_default_is_db(self, monkeypatch):
        # Вебхук стоит — значит getUpdates пуст ВСЕГДА. Умолчание обязано быть
        # базой, иначе после деплоя вебхука контур перестаёт применять молча.
        monkeypatch.delenv("APPROVER_SOURCE", raising=False)
        assert agent_approver.decision_source() == "db"

    def test_getupdates_is_the_rollback_path(self, monkeypatch):
        monkeypatch.setenv("APPROVER_SOURCE", "getupdates")
        assert agent_approver.decision_source() == "getupdates"

    def test_value_is_normalized(self, monkeypatch):
        monkeypatch.setenv("APPROVER_SOURCE", "  GetUpdates \n")
        assert agent_approver.decision_source() == "getupdates"


def _pending(action_id="3fa2b1c9", key="k1"):
    return {"action_id": action_id, "idempotency_key": key, "account": "acc",
            "object_id": "111", "action_kind": "campaign.suspend",
            "payload": {}, "risk_rub": 1500.0}


class TestVerdictsFromDb:
    def test_approved_and_vetoed_map_to_codes(self, monkeypatch):
        rows = [_pending("aaaaaa11", "k1"), _pending("bbbbbb22", "k2")]
        by_code = {approval.short_code(r["action_id"]): r for r in rows}
        monkeypatch.setattr(
            approval_db, "decisions_for",
            lambda keys: {"k1": approval_db.DECISION_APPROVED,
                          "k2": approval_db.DECISION_VETOED})
        assert agent_approver.verdicts_from_db(rows, by_code) == {
            "aaaaaa": True, "bbbbbb": False}

    def test_no_decision_means_no_verdict(self, monkeypatch):
        """Молчание — не «нет»: его закрывает TTL, а не воркер.

        Иначе действие, о котором человек ещё думает, закрывалось бы отказом на
        первом же прогоне после запроса.
        """
        rows = [_pending("aaaaaa11", "k1")]
        by_code = {"aaaaaa": rows[0]}
        monkeypatch.setattr(approval_db, "decisions_for", lambda keys: {})
        assert agent_approver.verdicts_from_db(rows, by_code) == {}

    def test_expired_decision_is_not_a_verdict(self, monkeypatch):
        # DECISION_EXPIRED пишет сам воркер, закрывая просрочку. Прочитать его
        # обратно как решение человека значило бы применить то, что уже закрыто.
        rows = [_pending("aaaaaa11", "k1")]
        by_code = {"aaaaaa": rows[0]}
        monkeypatch.setattr(
            approval_db, "decisions_for",
            lambda keys: {"k1": approval_db.DECISION_EXPIRED})
        assert agent_approver.verdicts_from_db(rows, by_code) == {}


class TestRequestButtons:
    def test_callback_data_matches_webhook_contract(self):
        """Формат «ok:<код>» / «no:<код>» разбирает lib/telegram/router.ts.

        Контракт живёт в двух репозиториях, поэтому он закреплён тестом с этой
        стороны: молчаливое расхождение означает кнопку, которая ничего не
        делает.
        """
        buttons = approval.request_buttons([{"action_id": "3fa2b1c9d0"}])
        assert buttons == [[
            {"text": "Применить 3fa2b1", "callback_data": "ok:3fa2b1"},
            {"text": "Отклонить 3fa2b1", "callback_data": "no:3fa2b1"},
        ]]

    def test_code_is_the_same_short_code_as_in_text(self):
        row = {"action_id": "abcdef0123456789"}
        code = approval.short_code(row["action_id"])
        data = approval.request_buttons([row])[0][0]["callback_data"]
        assert data == f"ok:{code}"

    def test_rows_without_action_id_are_skipped(self):
        assert approval.request_buttons([{"action_id": ""}, {}]) == []

    def test_callback_data_fits_bot_api_limit(self):
        # Bot API режет callback_data на 64 байтах — молча, без ошибки.
        for row in approval.request_buttons([{"action_id": "3fa2b1c9d0"}]):
            for button in row:
                assert len(button["callback_data"].encode("utf-8")) <= 64


class TestNotifyButtons:
    def test_buttons_go_as_reply_markup(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
        sent = []
        monkeypatch.setattr(notify, "_post", lambda url, data: sent.append(data))
        notify.send("очередь", [[{"text": "Применить", "callback_data": "ok:abc123"}]])
        assert b'"reply_markup"' in sent[0]
        assert b'"inline_keyboard"' in sent[0]

    def test_without_buttons_payload_has_no_markup(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
        sent = []
        monkeypatch.setattr(notify, "_post", lambda url, data: sent.append(data))
        notify.send("просто текст")
        assert b"reply_markup" not in sent[0]


class TestRunUsesSource:
    """run() в режиме базы не должен ходить в getUpdates вовсе."""

    def test_db_source_does_not_call_getupdates(self, monkeypatch):
        monkeypatch.delenv("APPROVER_SOURCE", raising=False)
        monkeypatch.setattr(approval_db, "ensure_schema", lambda: None)
        monkeypatch.setattr(approval_db, "expire_pending", lambda ttl: [])
        monkeypatch.setattr(approval_db, "load_pending", lambda: [])

        def _boom(offset):
            raise AssertionError("getUpdates не должен зваться при вебхуке")

        monkeypatch.setattr(agent_approver, "fetch_updates", _boom)
        report = agent_approver.run(dry_run=True)
        assert report["source"] == "db"
        assert report["applied"] == [] and report["vetoed"] == []

    def test_getupdates_source_still_reads_chat(self, monkeypatch):
        monkeypatch.setenv("APPROVER_SOURCE", "getupdates")
        monkeypatch.setattr(approval_db, "ensure_schema", lambda: None)
        monkeypatch.setattr(approval_db, "expire_pending", lambda ttl: [])
        monkeypatch.setattr(approval_db, "load_pending", lambda: [])
        monkeypatch.setattr(approval_db, "get_offset", lambda: 0)
        called = []
        monkeypatch.setattr(agent_approver, "fetch_updates",
                            lambda offset: called.append(offset) or [])
        report = agent_approver.run(dry_run=True)
        assert called == [0]
        assert report["source"] == "getupdates"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
