# -*- coding: utf-8 -*-
"""
sync/agent/writer/guardrails.py — рельсы движка записи (слой 2 защиты).

Ограничения, которые агент не пересекает никогда — независимо от того, что
насчитала статистика и что предложил LLM:

  - удаление объектов запрещено (пауза вместо удаления);
  - корректировка ставки ограничена ±50%: расчёт уже сжат к нулю, всё что
    выходит за коридор — признак ошибки, а не находки;
  - заповедник неприкосновенен: его кампании не получают ни одного действия,
    иначе база сравнения для всех замеров теряется;
  - число действий за прогон ограничено: массовое изменение невозможно
    проверить сторожем и невозможно осмысленно откатить.
"""

from typing import Any, Dict, List, Set, Tuple

MODIFIER_CAP = 50          # потолок и пол корректировки, проценты
MAX_ACTIONS_PER_RUN = 50


def check_action(action: Dict[str, Any]) -> Tuple[bool, str]:
    """Проверка одного действия. Возвращает (можно ли, причина отказа)."""
    kind = str(action.get("action_kind") or "")
    if "delete" in kind or "remove" in kind:
        return False, "удаление объектов запрещено: агент только паузит"

    percent = action.get("payload", {}).get("BidModifier")
    if percent is not None:
        if abs(int(percent)) > MODIFIER_CAP:
            return False, f"потолок корректировки ±{MODIFIER_CAP}%, получено {percent}%"
    return True, ""


def check_holdout(
    actions: List[Dict[str, Any]], holdout_ids: Set[str]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Разделяет действия на разрешённые и заблокированные заповедником."""
    allowed = [a for a in actions if str(a.get("object_id")) not in holdout_ids]
    blocked = [a for a in actions if str(a.get("object_id")) in holdout_ids]
    return allowed, blocked


def cap_actions(
    actions: List[Dict[str, Any]], max_per_run: int = MAX_ACTIONS_PER_RUN
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Ограничивает объём одного прогона. Остальное ждёт следующего."""
    return actions[:max_per_run], actions[max_per_run:]
