# -*- coding: utf-8 -*-
"""Апрув-контур Telegram (sync/agent/approval.py) — чистая логика.

Проверяется контракт между тремя участниками: Э1 (что встаёт в очередь),
человеком (как разбираются его слова) и воркером (какое действие какому
коду соответствует). Ошибка в любом из трёх — применение чужого действия
в боевом кабинете, поэтому разбор слов проверяется в обе стороны: что
матчится и что ОБЯЗАНО не матчиться.
"""

from sync.agent import approval


def _action(lane, key="k1", kind="campaign.suspend", vetoed=False):
    return {"lane": lane, "idempotency_key": key, "action_kind": kind,
            "object_id": "111", "account": "acc", "payload": {}}


class TestApprovalLanes:
    def test_default_lanes(self):
        assert approval.approval_lanes(None) == frozenset({"suspend", "allocation"})
        assert approval.approval_lanes({}) == frozenset({"suspend", "allocation"})
        # Пустое значение панели = «дефолт кода», как у остальных nullable.
        assert approval.approval_lanes({"approval_lanes": None}) == \
            frozenset({"suspend", "allocation"})

    def test_panel_overrides(self):
        got = approval.approval_lanes({"approval_lanes": ["suspend"]})
        assert got == frozenset({"suspend"})


class TestSplitForApproval:
    def test_approval_lane_goes_to_hold(self):
        actions = [_action("suspend", "a"), _action("hygiene", "b"),
                   _action("allocation", "c")]
        now, hold = approval.split_for_approval(actions, None)
        assert [a["idempotency_key"] for a in now] == ["b"]
        assert [a["idempotency_key"] for a in hold] == ["a", "c"]

    def test_vetoed_marked_not_queued(self):
        actions = [_action("suspend", "a"), _action("suspend", "b")]
        now, hold = approval.split_for_approval(actions, None, vetoed_keys=["a"])
        assert now == []
        by_key = {a["idempotency_key"]: a for a in hold}
        assert by_key["a"].get("_vetoed") is True
        assert "_vetoed" not in by_key["b"]

    def test_veto_does_not_leak_into_original(self):
        # split возвращает копию с меткой, не мутирует вход: тот же dict
        # ниже по прогону едет в отказы, и чужое поле там — сюрприз.
        action = _action("suspend", "a")
        approval.split_for_approval([action], None, vetoed_keys=["a"])
        assert "_vetoed" not in action

    def test_lane_outside_list_never_held(self):
        actions = [_action("tuning", "a"), _action("hygiene", "b")]
        now, hold = approval.split_for_approval(actions, None)
        assert len(now) == 2 and hold == []


class TestParseDecisions:
    def test_yes_and_no_with_codes(self):
        got = approval.parse_decisions(["да 3fa2b1", "нет 8c41d0"])
        assert got == [("3fa2b1", True), ("8c41d0", False)]

    def test_multiple_codes_one_message(self):
        got = approval.parse_decisions(["да 3fa2b1 8c41d0"])
        assert got == [("3fa2b1", True), ("8c41d0", True)]

    def test_all_keyword(self):
        assert approval.parse_decisions(["да все"]) == [("*", True)]
        assert approval.parse_decisions(["нет всё"]) == [("*", False)]

    def test_plain_chat_ignored(self):
        # Живой чат: обычные сообщения не становятся решениями.
        assert approval.parse_decisions(["привет", "как дела", ""]) == []
        # «да» без кода — не решение ни о чём.
        assert approval.parse_decisions(["да"]) == []

    def test_code_without_verdict_ignored(self):
        assert approval.parse_decisions(["3fa2b1"]) == []

    def test_case_and_commas(self):
        got = approval.parse_decisions(["ДА 3FA2B1, 8C41D0"])
        assert got == [("3fa2b1", True), ("8c41d0", True)]

    def test_later_word_switches_verdict(self):
        got = approval.parse_decisions(["да 3fa2b1 нет 8c41d0"])
        assert got == [("3fa2b1", True), ("8c41d0", False)]


class TestResolveDecisions:
    CODES = ["3fa2b1", "8c41d0", "8c99ff"]

    def test_exact_match(self):
        got = approval.resolve_decisions([("3fa2b1", True)], self.CODES)
        assert got == {"3fa2b1": True}

    def test_prefix_match_unique(self):
        got = approval.resolve_decisions([("3fa2", False)], self.CODES)
        assert got == {"3fa2b1": False}

    def test_ambiguous_prefix_matches_nothing(self):
        # «8c41» и «8c99» оба начинаются с «8c» — применить чужое действие
        # хуже, чем переспросить молчанием.
        got = approval.resolve_decisions([("8c", True)], self.CODES)
        assert got == {}

    def test_star_covers_all(self):
        got = approval.resolve_decisions([("*", False)], self.CODES)
        assert got == {c: False for c in self.CODES}

    def test_later_decision_wins(self):
        got = approval.resolve_decisions(
            [("3fa2b1", True), ("3fa2b1", False)], self.CODES)
        assert got == {"3fa2b1": False}

    def test_unknown_code_ignored(self):
        assert approval.resolve_decisions([("ffffff", True)], self.CODES) == {}


class TestFormatRequest:
    def test_contains_codes_and_instruction(self):
        rows = [{"action_id": "3fa2b1deadbeef", "object_id": "111",
                 "account": "acc", "action_kind": "campaign.suspend",
                 "payload": {}, "risk_rub": 1234.0}]
        text = approval.format_request(rows)
        assert "[3fa2b1]" in text
        assert "пауза кампании" in text
        assert "111" in text
        assert "да <код>" in text and "нет <код>" in text
        assert str(approval.PENDING_TTL_HOURS) in text

    def test_budget_amount_in_rubles(self):
        rows = [{"action_id": "abcdef012345", "object_id": "222",
                 "account": "acc", "action_kind": "budget.set",
                 "payload": {"BiddingStrategy": {"Search": {
                     "WeeklySpendLimit": 70_000_000_000}}},
                 "risk_rub": 0.0}]
        text = approval.format_request(rows)
        # 70 000 000 000 микрорублей = 70 000 ₽ — в сводке рубли, не микро.
        assert "70 000" in text
        assert "70 000 000 000" not in text

    def test_zero_risk_has_no_risk_note(self):
        rows = [{"action_id": "abcdef012345", "object_id": "222",
                 "account": "acc", "action_kind": "goal.set",
                 "payload": {}, "risk_rub": 0.0}]
        assert "риск" not in approval.format_request(rows)


class TestRoundTrip:
    def test_request_to_decision_round_trip(self):
        # Сквозной путь: код из сводки, набранный человеком, находит ровно
        # то действие, которое в сводке стояло.
        rows = [{"action_id": "3fa2b1deadbeef", "object_id": "111",
                 "account": "acc", "action_kind": "campaign.suspend",
                 "payload": {}, "risk_rub": 500.0},
                {"action_id": "8c41d0deadbeef", "object_id": "222",
                 "account": "acc", "action_kind": "budget.set",
                 "payload": {}, "risk_rub": 900.0}]
        codes = [approval.short_code(r["action_id"]) for r in rows]
        decisions = approval.parse_decisions(["да 3fa2b1", "нет 8c41d0"])
        verdicts = approval.resolve_decisions(decisions, codes)
        assert verdicts == {"3fa2b1": True, "8c41d0": False}


class TestResumeGist:
    def test_resume_names_the_campaign_and_says_it_is_paused(self):
        row = {"action_id": "abc123deadbeef", "object_id": "987654",
               "account": "acc-edu", "action_kind": "campaign.resume",
               "payload": {"CampaignId": 987654,
                           "CampaignName": "EDU_CONS_MSK",
                           "order_id": "ord-1"},
               "risk_rub": 0.0}
        text = approval.format_request([row])
        assert "ВКЛЮЧИТЬ" in text
        assert "EDU_CONS_MSK" in text
        assert "на паузе" in text
        # Риска нет — рублей в строке быть не должно.
        assert "риск" not in text


class TestCloseResumedOrder:
    """Учёт включения: «да» на campaign.resume закрывает наряд датой."""

    def _row(self, payload):
        return {"action_id": "a" * 24, "object_id": "987654",
                "action_kind": "campaign.resume", "payload": payload}

    def test_applied_resume_accepts_the_order_with_todays_date(self, monkeypatch):
        from sync import agent_approver

        calls = {}

        def _accept(order_id, campaign_id, started_on, note):
            calls.update(order_id=order_id, campaign_id=campaign_id,
                         started_on=started_on)
            return {"order_id": order_id, "experiment_id": "exp-1"}

        monkeypatch.setattr(agent_approver.build_queue, "accept", _accept)
        out = agent_approver._close_resumed_order(
            self._row({"order_id": "ord-1", "CampaignId": 987654}))
        assert out["closed"] is True
        assert out["experiment_id"] == "exp-1"
        assert calls["order_id"] == "ord-1"
        assert calls["campaign_id"] == "987654"
        assert calls["started_on"]  # сегодняшняя дата, не пусто

    def test_payload_arrives_as_json_text_from_the_journal(self, monkeypatch):
        # Колонка payload — jsonb; часть драйверов отдаёт её строкой.
        from sync import agent_approver

        monkeypatch.setattr(
            agent_approver.build_queue, "accept",
            lambda order_id, **k: {"order_id": order_id})
        out = agent_approver._close_resumed_order(
            self._row('{"order_id": "ord-2", "CampaignId": 1}'))
        assert out["closed"] is True

    def test_accounting_failure_does_not_hide_the_resume(self, monkeypatch):
        # Кампания уже включена — отказ учёта виден полем, не исключением.
        from sync import agent_approver

        def _boom(*a, **k):
            raise RuntimeError("база недоступна")

        monkeypatch.setattr(agent_approver.build_queue, "accept", _boom)
        out = agent_approver._close_resumed_order(self._row({"order_id": "x"}))
        assert out["closed"] is False
        assert "RuntimeError" in out["reason"]

    def test_missing_order_id_is_named_not_swallowed(self, monkeypatch):
        from sync import agent_approver

        out = agent_approver._close_resumed_order(self._row({}))
        assert out["closed"] is False
        assert "order_id" in out["reason"]
