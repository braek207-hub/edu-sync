# -*- coding: utf-8 -*-
"""Сравнение текстов в модулях: ё=е и слова запроса по порядку.

Общие для queries и движка правил: разойдись свёртка или разбор на слова,
правило и встроенная защита по-разному поняли бы один и тот же запрос.
"""

from typing import Any, List

from sync.agent.objects import _WORD_RE


def yo(text: str) -> str:
    # Директ не различает ё и е: «учеба» в минусе гасит ключ «учёба». Только для сравнений,
    # записываемая фраза остаётся как есть.
    return text.replace("ё", "е")


def word_list(text: Any) -> List[str]:
    # Разбор тот же, что у агента (objects._phrase_words), но с порядком: подфразе он нужен.
    return _WORD_RE.findall(str(text or "").lower())
