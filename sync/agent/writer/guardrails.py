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

# Рельса работает по ДЕЛЬТЕ, а не по 100-базному коэффициенту Директа: в
# payload действия (diff.py) лежит внутренняя единица движка, перевод в шкалу
# API делается позже и только один раз — в apply.to_api_call через
# units.delta_to_api. Смысл рельсы: не выпускать корректировку за ±50 % от
# исходной ставки, то есть |дельта| <= 50 (в шкале API это коридор 50..150).
# Та же цифра, приложенная к 100-базе, разрешала бы 0..50 — «срезать ставку
# вдвое и сильнее», ровно наоборот к замыслу.
MODIFIER_CAP = 50          # потолок и пол корректировки, проценты (дельта)
MAX_ACTIONS_PER_RUN = 50

# Allow-лист: разрешено ровно это, всё остальное отклоняется. Не блок-лист по
# словам — тот пропускает любой ещё не придуманный вид действия (purge,
# campaign.archive, adgroups.suspend, ...) молча, а рельса обязана держать
# "никогда", а не эвристику по подстроке.
ALLOWED_ACTION_KINDS = {"bidmodifier.add", "bidmodifier.set"}


def check_action(action: Dict[str, Any]) -> Tuple[bool, str]:
    """Проверка одного действия. Возвращает (можно ли, причина отказа)."""
    kind = str(action.get("action_kind") or "")
    kind_lower = kind.lower()

    # Отдельная явная проверка поверх allow-листа — не для защиты (её уже
    # даёт allow-лист), а чтобы в журнале была понятная причина отказа
    # именно "удаление", а не общая "вне allow-листа".
    if "delete" in kind_lower or "remove" in kind_lower:
        return False, "удаление объектов запрещено: агент только паузит"

    if kind not in ALLOWED_ACTION_KINDS:
        return False, f"вид действия вне allow-листа: {kind}"

    percent = action.get("payload", {}).get("BidModifier")
    if percent is not None:
        if abs(int(percent)) > MODIFIER_CAP:
            return False, f"потолок корректировки ±{MODIFIER_CAP}%, получено {percent}%"
    return True, ""


def check_holdout(
    actions: List[Dict[str, Any]], holdout_ids: Set[Any]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Разделяет действия на разрешённые и заблокированные заповедником."""
    holdout_ids = {str(h) for h in holdout_ids}
    allowed = [a for a in actions if str(a.get("object_id")) not in holdout_ids]
    blocked = [a for a in actions if str(a.get("object_id")) in holdout_ids]
    return allowed, blocked


def cap_actions(
    actions: List[Dict[str, Any]], max_per_run: int = MAX_ACTIONS_PER_RUN
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Ограничивает объём одного прогона. Остальное ждёт следующего."""
    return actions[:max_per_run], actions[max_per_run:]
