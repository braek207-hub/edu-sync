# -*- coding: utf-8 -*-
"""
sync/agent/writer/rollback.py — красные линии и автооткат (слой 3 защиты).

Автооткат функционально заменяет апрув: вместо «человек предотвращает ошибку
заранее» — «система исправляет её за час». Для рекламы это строго лучше:
результат изменения человек всё равно не предсказывает.

Красная линия ставится вместе с действием, а не после: у каждого применённого
изменения заранее известно, при каком исходе оно считается провалом.

Откат никогда не удаляет: даже отмена добавленной корректировки — это
установка нейтральных 0%, а не delete.

Идентификатор корректировки для отката всегда берётся из ответа API
(result.AddResults[].Id для bidmodifier.add), а не придумывается: Id=0 не
существует в Директе, и запрос с ним молча ничего не откатит. Если Id
неизвестен ни в payload, ни в сохранённом response действия — откат
невозможен, и функция явно возвращает None вместо запроса вслепую.
"""

from typing import Any, Dict, Optional, Tuple

RED_LINE_TOLERANCE = 0.40   # +40% к базовой метрике
MIN_LEADS_FOR_VERDICT = 20  # до этого объёма вывод делать нельзя


def red_line_for(action: Dict[str, Any], baseline: Dict[str, Any]) -> Dict[str, Any]:
    """Условие, при котором изменение считается провалом."""
    base_cpa = float(baseline.get("cpa") or 0.0)
    return {
        "metric": "cpa",
        "max_value": round(base_cpa * (1 + RED_LINE_TOLERANCE), 2),
        "min_leads": MIN_LEADS_FOR_VERDICT,
        "baseline_cpa": base_cpa,
    }


def is_breached(red_line: Dict[str, Any], observed: Dict[str, Any]) -> Tuple[bool, str]:
    """Пробита ли красная линия. До минимума наблюдений — никогда."""
    leads = int(observed.get("leads") or 0)
    if leads < int(red_line.get("min_leads") or MIN_LEADS_FOR_VERDICT):
        return False, f"недостаточно наблюдений: {leads}"
    value = float(observed.get(red_line.get("metric", "cpa")) or 0.0)
    limit = float(red_line.get("max_value") or 0.0)
    if limit and value > limit:
        return True, f"{red_line['metric']} = {value:.0f} при пределе {limit:.0f}"
    return False, ""


def _added_modifier_id(action: Dict[str, Any]) -> Optional[Any]:
    """Id корректировки, добавленной действием bidmodifier.add.

    Директ не позволяет указать Id при add — он приходит только в ответе
    (result.AddResults[].Id) и сохраняется в журнале действий в поле
    response. payload.Id проверяется на случай, если вызывающий код уже
    дописал туда присвоенный Id заранее — сам rollback_payload его не
    придумывает.
    """
    payload = action.get("payload") or {}
    if payload.get("Id") is not None:
        return payload["Id"]

    response = action.get("response") or {}
    add_results = response.get("AddResults") or []
    if add_results and isinstance(add_results, list):
        first = add_results[0] or {}
        return first.get("Id")
    return None


def rollback_payload(action: Dict[str, Any]) -> Optional[Tuple[str, str, Dict[str, Any]]]:
    """Запрос, возвращающий объект в прошлое состояние.

    Ничего не удаляет: отмена add — это set нейтрального 0%, а не delete.
    Без известного Id откат невозможен — вслепую не отправляем.
    """
    kind = str(action.get("action_kind") or "")
    previous = action.get("previous_state") or {}

    if kind == "bidmodifier.set" and previous.get("Id") is not None:
        return "bidmodifiers", "set", {
            "BidModifiers": [{"Id": previous["Id"], "BidModifier": int(previous["percent"])}]
        }

    if kind == "bidmodifier.add":
        modifier_id = _added_modifier_id(action)
        if modifier_id is None:
            return None
        return "bidmodifiers", "set", {
            "BidModifiers": [{"Id": modifier_id, "BidModifier": 0}]
        }

    return None
