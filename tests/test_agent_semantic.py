# -*- coding: utf-8 -*-
"""Смысловой фильтр над кандидатами рычагов (sync/agent/semantic.py).

Статистика видит только деньги и конверсии: фраза с пятью кликами и нулём
конверсий для неё неотличима от фразы, которая просто не набрала объём. На
живых данных 25.08 в этой зоне лежало 43,5 млн ₽ расхода — 96 % всех денег
на запросах без конверсий. Смысл эту границу видит: «скачать реферат» —
мусор при любом объёме, «высшее образование дистанционно» — ядро при любой
цене.

Модель здесь ПРЕДЛАГАЕТ, а решает по-прежнему арифметика: вердикт модели
может только ЗАПРЕТИТЬ действие, но не назначить его.
"""

import json

from sync.agent.semantic import (
    UNCLEAR,
    build_prompt,
    classify,
    keep_minus_candidates,
    parse_response,
)


def _ask(payload):
    """Фейковая модель: отвечает строгим JSON, как настоящая."""
    return json.dumps({"verdicts": [
        {"query": "скачать реферат бесплатно", "verdict": "junk",
         "reason": "нецелевой интент: ищут файл, а не обучение"},
        {"query": "высшее образование дистанционно", "verdict": "core",
         "reason": "прямой интент нашего продукта"},
    ]}, ensure_ascii=False)


def test_classify_returns_verdict_per_query():
    out = classify(["скачать реферат бесплатно", "высшее образование дистанционно"],
                   ask=_ask, context="онлайн-образование")
    assert out["скачать реферат бесплатно"]["verdict"] == "junk"
    assert out["высшее образование дистанционно"]["verdict"] == "core"


def test_unanswered_query_is_unclear_not_junk():
    # Модель ответила не про всё — умолчание безопасное: «неясно», а не
    # «мусор». Иначе молчание модели молча запрещало бы трафик.
    out = classify(["фраза без ответа"], ask=_ask, context="онлайн-образование")
    assert out["фраза без ответа"]["verdict"] == UNCLEAR


def test_broken_response_does_not_break_the_run():
    # Модель вернула не JSON. Прогон обязан продолжиться со всеми «неясно»:
    # смысловой слой — надстройка, его отказ не останавливает рычаг.
    out = classify(["фраза"], ask=lambda payload: "извините, не могу",
                   context="онлайн-образование")
    assert out["фраза"]["verdict"] == UNCLEAR


def test_model_can_only_veto_never_add():
    # Ключевое правило: модель отсеивает кандидатов статистики, но своих не
    # добавляет. «junk» без экономического основания — не действие.
    stat = [{"query": "скачать реферат бесплатно", "cost": 5000.0},
            {"query": "высшее образование дистанционно", "cost": 90_000.0}]
    verdicts = classify([c["query"] for c in stat], ask=_ask,
                        context="онлайн-образование")
    kept = keep_minus_candidates(stat, verdicts)
    assert [c["query"] for c in kept] == ["скачать реферат бесплатно"]
    assert all("semantic" in c for c in kept)


def test_unclear_candidates_still_apply():
    # Модель недоступна или промолчала — поведение прежнее: решает статистика.
    stat = [{"query": "какая-то фраза", "cost": 5000.0}]
    kept = keep_minus_candidates(stat, {})
    assert len(kept) == 1


def test_prompt_carries_context_and_queries():
    prompt = build_prompt(["фраза один", "фраза два"], context="онлайн-образование")
    assert "онлайн-образование" in prompt
    assert "фраза один" in prompt and "фраза два" in prompt
    # Формат ответа задан жёстко: разбор не должен зависеть от фантазии модели.
    assert "json" in prompt.lower()


def test_parse_response_tolerates_code_fences():
    raw = '```json\n{"verdicts": [{"query": "a", "verdict": "junk"}]}\n```'
    assert parse_response(raw)["a"]["verdict"] == "junk"
