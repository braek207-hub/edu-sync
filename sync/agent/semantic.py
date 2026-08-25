# -*- coding: utf-8 -*-
"""
sync/agent/semantic.py — смысловой слой над кандидатами рычагов.

Арифметика видит только деньги и конверсии. Фраза с пятью кликами и нулём
конверсий для неё неотличима от фразы, которая просто не набрала объём: при
базовой конверсии 3,6 % приговор требует 83 кликов, а хвост живёт на трёх-
двадцати. Замер 25.08: расход на запросах без единой конверсии — 45,5 млн ₽
за шесть недель, из них статистика способна судить 208 фраз на 2,08 млн ₽.
Остальные 43,5 млн — зона, где решает СМЫСЛ, а не объём.

Смысл эту границу видит сразу: «скачать реферат» — мусор при любом объёме,
«высшее образование дистанционно» — ядро при любой цене. Ровно этого не
хватило 25.08, когда рычаг предложил заминусовать «высшее», «институты» и
«факультеты»: защиту пришлось строить правилами (ноль конверсий + слова
собственных ключевых фраз), а правила описывают частные случаи.

ГЛАВНОЕ ПРАВИЛО СЛОЯ: модель может только ЗАПРЕТИТЬ действие, но не назначить
его. Кандидатов по-прежнему отбирает экономика; вердикт "core" снимает
кандидата с минусации, вердикт "junk" снимает его с расширения. «Модель
сказала мусор» само по себе не основание тратить или резать деньги —
галлюцинация в этой позиции стоила бы кампании.

Второе правило: отказ слоя не останавливает рычаг. Нет ключа, сеть легла,
ответ не разобрался — все вердикты становятся UNCLEAR, и поведение ровно
такое же, как до появления модуля: решает статистика.
"""

import json
import os
import re
from typing import Any, Callable, Dict, Iterable, List, Optional

# Вердикты. UNCLEAR — безопасное умолчание: молчание модели не должно ни
# запрещать трафик, ни разрешать трату.
JUNK = "junk"
CORE = "core"
UNCLEAR = "unclear"

KNOWN_VERDICTS = (JUNK, CORE, UNCLEAR)

# Сколько фраз уходит в один запрос. Батч крупнее экономит вызовы, но длинный
# ответ модель чаще обрывает на середине, а оборванный JSON — это UNCLEAR для
# всего батча разом.
BATCH_SIZE = 40

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def build_prompt(queries: Iterable[str], context: str) -> str:
    """Запрос к модели: контекст проекта + список фраз + жёсткий формат ответа.

    Формат задан явно и с примером, потому что разбор не должен зависеть от
    фантазии модели: свободный текст здесь означает UNCLEAR для всего батча.
    """
    listed = "\n".join(f"- {q}" for q in queries)
    return (
        "Ты — маркетолог контекстной рекламы. Проект: "
        f"{context}.\n\n"
        "Для каждой поисковой фразы ниже реши, что это за интент:\n"
        "  core — человек ищет ровно наш продукт или близкое к нему; такую\n"
        "         фразу нельзя запрещать ни при какой цене;\n"
        "  junk — интент нецелевой (ищут файл, работу, чужой город, чужой\n"
        "         продукт, справку вместо обучения); деньги на неё уходят зря;\n"
        "  unclear — по фразе нельзя судить уверенно.\n\n"
        "Сомневаешься — отвечай unclear: ошибочный junk отрезает живой трафик.\n\n"
        f"Фразы:\n{listed}\n\n"
        "Ответь ТОЛЬКО валидным JSON, без пояснений вокруг, строго в виде:\n"
        '{"verdicts": [{"query": "<фраза>", "verdict": "core|junk|unclear", '
        '"reason": "<коротко, почему>"}]}'
    )


def parse_response(raw: str) -> Dict[str, Dict[str, Any]]:
    """Ответ модели → {фраза: {verdict, reason}}. Мусор на входе → пустой словарь.

    Терпит обёртку в ```-блок: модели её добавляют, несмотря на просьбу.
    Неизвестный вердикт трактуется как UNCLEAR — набор значений закрытый,
    и «почти core» здесь не бывает.
    """
    if not raw:
        return {}
    text = _FENCE.sub("", str(raw)).strip()
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return {}
    rows = data.get("verdicts") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        query = str(row.get("query") or "").strip().lower()
        if not query:
            continue
        verdict = str(row.get("verdict") or "").strip().lower()
        out[query] = {
            "verdict": verdict if verdict in KNOWN_VERDICTS else UNCLEAR,
            "reason": str(row.get("reason") or "")[:200],
        }
    return out


def classify(
    queries: Iterable[str], ask: Callable[[str], str], context: str,
    batch_size: int = BATCH_SIZE,
) -> Dict[str, Dict[str, Any]]:
    """Вердикты по фразам. Всё, о чём модель не ответила, — UNCLEAR.

    ask — функция «промпт → текст ответа». Инъекция, а не прямой вызов сети:
    так слой тестируется без ключа и без интернета, а замена модели не
    трогает правила применения.

    Исключение любого вызова гасится: смысловой слой — надстройка, и его
    падение не имеет права ронять расчёт, который считает деньги.
    """
    listed = [str(q).strip().lower() for q in queries if str(q).strip()]
    out: Dict[str, Dict[str, Any]] = {
        q: {"verdict": UNCLEAR, "reason": "модель не ответила"} for q in listed}
    for start in range(0, len(listed), batch_size):
        batch = listed[start:start + batch_size]
        try:
            answered = parse_response(ask(build_prompt(batch, context)))
        except Exception as exc:  # noqa: BLE001 — см. докстринг
            answered = {}
            for q in batch:
                out[q] = {"verdict": UNCLEAR,
                          "reason": f"слой недоступен: {type(exc).__name__}"}
        for query, verdict in answered.items():
            if query in out:
                out[query] = verdict
    return out


def unclear_reasons(verdicts: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
    """Почему вердикты вышли неопределёнными — счётчик по причинам.

    UNCLEAR приходит тремя разными путями: модель честно ответила «не знаю»,
    ответ не разобрался, вызов упал. Для рычага это одно и то же (вето нет),
    для человека — совершенно разные новости: первое значит, что слой
    работает, а два других — что он молчит, и молчание выглядит как согласие.
    Одна строка счётчиков в отчёте отличает их без чтения логов.
    """
    out: Dict[str, int] = {}
    for verdict in verdicts.values():
        if verdict.get("verdict") != UNCLEAR:
            continue
        reason = str(verdict.get("reason") or "").strip() or "без причины"
        out[reason[:60]] = out.get(reason[:60], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def keep_minus_candidates(
    candidates: List[Dict[str, Any]], verdicts: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Кандидаты в минус, из которых убраны признанные СВОЕЙ семантикой.

    Вето, а не отбор: UNCLEAR и отсутствие вердикта оставляют кандидата в
    силе — решает экономика, как и до появления слоя.
    """
    kept: List[Dict[str, Any]] = []
    for candidate in candidates:
        query = str(candidate.get("query") or "").strip().lower()
        verdict = verdicts.get(query) or {}
        if verdict.get("verdict") == CORE:
            continue
        kept.append({**candidate, "semantic": verdict.get("verdict", UNCLEAR)})
    return kept


def keep_expansion_candidates(
    candidates: List[Dict[str, Any]], verdicts: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Кандидаты на расширение без тех, что признаны мусорным интентом.

    Зеркало keep_minus_candidates: там вето накладывает "core", здесь —
    "junk". Случай из живых данных: «диплом о высшем образовании купить»
    даёт дешёвые заявки, и арифметика хочет их докупить; смысл видит, что
    это не наш покупатель.
    """
    kept: List[Dict[str, Any]] = []
    for candidate in candidates:
        query = str(candidate.get("query") or "").strip().lower()
        verdict = verdicts.get(query) or {}
        if verdict.get("verdict") == JUNK:
            continue
        kept.append({**candidate, "semantic": verdict.get("verdict", UNCLEAR)})
    return kept


def deepseek_asker(
    api_key: Optional[str] = None, model: str = "deepseek-chat",
    timeout: int = 120,
) -> Optional[Callable[[str], str]]:
    """Функция обращения к модели, или None, если ключа нет.

    None — законное состояние: без ключа прогон работает ровно как раньше,
    и это видно в отчёте отдельной строкой, а не молчанием.
    """
    key = api_key or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return None

    def _ask(prompt: str) -> str:
        import requests

        # Тело кодируется в UTF-8 ЗДЕСЬ, а не отдаётся строкой параметру
        # json=: строку по дороге кодируют в latin-1, и первая же кириллица
        # даёт UnicodeEncodeError. Слой ошибку глотает и возвращает UNCLEAR
        # по всему батчу — то есть молчит ровно так же, как без ключа, и это
        # молчание неотличимо от согласия. Ровно так он и молчал в первом
        # боевом прогоне: 12 вердиктов из 12, все «слой недоступен».
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            # Ноль температуры: разметка обязана быть воспроизводимой,
            # иначе один и тот же кандидат в разных прогонах получает
            # разные вердикты и рычаг дрожит.
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }, ensure_ascii=False).encode("utf-8")
        response = requests.post(
            "https://api.deepseek.com/chat/completions",
            data=payload,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json; charset=utf-8"},
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]

    return _ask
