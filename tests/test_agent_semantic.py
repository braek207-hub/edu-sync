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

from sync.agent import semantic
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


def test_unclear_reasons_separate_silence_from_ignorance():
    # Для рычага все три случая одинаковы — вето нет. Для человека это
    # «слой работает» против «слой молчит, и молчание похоже на согласие».
    verdicts = {
        "а": {"verdict": semantic.UNCLEAR, "reason": "слой недоступен: ReadTimeout"},
        "б": {"verdict": semantic.UNCLEAR, "reason": "слой недоступен: ReadTimeout"},
        "в": {"verdict": semantic.UNCLEAR, "reason": "модель не ответила"},
        "г": {"verdict": semantic.CORE, "reason": "ровно наш продукт"},
    }

    assert semantic.unclear_reasons(verdicts) == {
        "слой недоступен: ReadTimeout": 2, "модель не ответила": 1}


def test_unclear_reasons_are_sorted_by_weight():
    verdicts = {
        "а": {"verdict": semantic.UNCLEAR, "reason": "редкая"},
        "б": {"verdict": semantic.UNCLEAR, "reason": "частая"},
        "в": {"verdict": semantic.UNCLEAR, "reason": "частая"},
    }

    assert list(semantic.unclear_reasons(verdicts)) == ["частая", "редкая"]


def test_verdict_without_a_reason_is_still_counted():
    # Причины нет — строка всё равно обязана быть видна: пропуск сделал бы
    # сумму причин меньше числа unclear, и разница читалась бы как ноль.
    verdicts = {"а": {"verdict": semantic.UNCLEAR}}

    assert semantic.unclear_reasons(verdicts) == {"без причины": 1}


def test_request_body_is_utf8_bytes(monkeypatch):
    # Фразы кириллические все до одной. Тело, ушедшее строкой, по дороге
    # кодируется в latin-1 — UnicodeEncodeError, весь батч становится
    # UNCLEAR, и слой молчит неотличимо от «ключа нет». Так он и молчал в
    # первом боевом прогоне: 12 из 12.
    import requests

    sent = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '{"verdicts": []}'}}]}

    def _fake_post(url, **kwargs):
        sent.update(kwargs)
        return _Response()

    monkeypatch.setattr(requests, "post", _fake_post)

    semantic.deepseek_asker(api_key="ключ-проверки")("скачать реферат бесплатно")

    assert "json" not in sent, "тело обязано уходить готовыми байтами"
    assert isinstance(sent["data"], bytes)
    body = json.loads(sent["data"].decode("utf-8"))
    assert "скачать реферат бесплатно" in body["messages"][0]["content"]


def test_non_ascii_body_survives_a_latin1_only_transport(monkeypatch):
    # Проверка у ПОЛУЧАТЕЛЯ: транспорт, умеющий только latin-1, — это и есть
    # http.client. Байты он пропускает, строку с кириллицей — нет.
    import requests

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "{}"}}]}

    def _latin1_only_post(url, **kwargs):
        body = kwargs.get("data")
        if isinstance(body, str):
            body.encode("latin-1")      # то же исключение, что в проде
        return _Response()

    monkeypatch.setattr(requests, "post", _latin1_only_post)

    semantic.deepseek_asker(api_key="k")("высшее образование дистанционно")


def test_invisible_characters_in_the_key_do_not_break_the_call(monkeypatch):
    # BOM и переводы строки цепляются к секрету от конвейера, которым его
    # заливали. Заголовки HTTP кодируются в latin-1, поэтому один такой
    # символ роняет ВЕСЬ батч, а слой при этом молчит ровно так же, как без
    # ключа. Сутки тишины стоили именно этого.
    import requests

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "{}"}}]}

    def _latin1_headers_post(url, **kwargs):
        for value in (kwargs.get("headers") or {}).values():
            value.encode("latin-1")     # то же, что делает http.client
        return _Response()

    monkeypatch.setattr(requests, "post", _latin1_headers_post)

    semantic.deepseek_asker(api_key="﻿sk-ключ\n".replace("ключ", "abc"))("фраза")


def test_a_key_of_only_invisible_characters_counts_as_no_key():
    # Пустой после чистки ключ — это «ключа нет», а не «ключ есть, но
    # сломан»: отчёт обязан сказать про отсутствие, а не молчать вердиктами.
    assert semantic.deepseek_asker(api_key="﻿\n ") is None
