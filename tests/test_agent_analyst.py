# -*- coding: utf-8 -*-
"""Надзорный слой: модель читает решения кода (sync/agent/analyst.py).

Этап 1 — read-only: слой производит суждения, не власть. Тесты держат два
инварианта: отказ модели не ломает ничего (None вместо исключения) и
сообщение владельцу не теряет счётчики суждений при любом размере разбора.
"""

import json

from sync.agent import analyst


def test_parse_accepts_json_wrapped_in_prose():
    raw = ('Вот разбор:\n{"digest": "день прошёл", "would_veto": [],'
           '"beyond_gates": [{"proposal": "поднять бюджет", "why": "спрос"}],'
           '"rule_issues": []}\nКонец.')
    result = analyst.parse_response(raw)
    assert result["digest"] == "день прошёл"
    assert result["beyond_gates"][0]["proposal"] == "поднять бюджет"


def test_parse_garbage_is_skip_not_crash():
    # Отказ слоя не трогает агента: мусор, пустота, JSON без разбора —
    # всё это SKIPPED, а не исключение посреди прогона.
    assert analyst.parse_response("модель ушла думать") is None
    assert analyst.parse_response("") is None
    assert analyst.parse_response('{"would_veto": []}') is None
    assert analyst.parse_response('{"digest": "ок", "would_veto": "не список"}'
                                  )["would_veto"] == []


def test_telegram_message_keeps_counters_over_digest_tail():
    # Хвост со счётчиками — навигация владельца по журналу; при длинном
    # разборе режется текст, а не счётчики.
    result = {
        "digest": "х" * 10_000,
        "would_veto": [{"action_id": "a1", "reason": "р" * 500}],
        "beyond_gates": [],
        "rule_issues": [],
    }
    message = analyst.format_telegram(result)
    assert len(message) <= analyst.TELEGRAM_LIMIT
    assert "вето-кандидатов 1" in message
    assert "a1" in message


def test_run_reports_are_clipped_honestly():
    fat = {"stage": "e0", "mode": "apply", "started_at": "2026-09-03",
           "verdict": None, "report": {"rows": ["x" * 100] * 500}}
    thin = {"stage": "drift", "mode": "compute", "started_at": "2026-09-03",
            "verdict": "GREEN", "report": {"ok": True}}
    compact = analyst.compact_runs([fat, thin])
    # Обрыв помечен: модель не должна принять усечённый отчёт за полный.
    assert "обрезано" in compact[0]["report"]
    assert len(compact[0]["report"]) < 9200
    assert json.loads(compact[1]["report"]) == {"ok": True}


def test_prompt_carries_every_block_and_the_ask():
    context = {
        "as_of": "2026-09-03",
        "runs": [{"stage": "e0"}], "actions": [{"action_id": "a1"}],
        "rejects": [{"reason": "budget"}], "ideas": [{"lane": "growth"}],
        "config": [{"key": "shadow_lanes"}], "risk_budget": {"limit_rub": 200000},
    }
    prompt = analyst.build_user_prompt(context)
    for marker in ("Прогоны агента", "Действия", "отказов", "Идеи",
                   "Панель", "Риск-бюджет", "2026-09-03", "JSON"):
        assert marker in prompt


def test_action_compaction_caps_the_list():
    actions = [{"action_id": f"a{i}", "action_kind": "bidmodifier.set",
                "object_level": "campaign", "object_id": str(i),
                "payload": {}, "previous_state": {}, "risk_rub": 1.0,
                "status": "applied", "account": "acc"}
               for i in range(analyst.MAX_ACTIONS + 10)]
    assert len(analyst.compact_actions(actions)) == analyst.MAX_ACTIONS
