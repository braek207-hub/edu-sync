# -*- coding: utf-8 -*-
"""
sync/agent/conflicts.py — рычаги, тянущие один объект в разные стороны.

Каждый рычаг считается отдельно и по-своему: бюджет смотрит на недобор
трафика, цель — на фактический CPA, выключатель — на провал кампании. По
отдельности любое их решение законно. Вместе на одной кампании они образуют
состояния, которых никто не задумывал:

  • лимит поднимаем, цель конверсии опускаем — деньги даём и тут же лишаем
    стратегию возможности их потратить; кампания стоит на месте, а риск
    списан дважды;
  • кампанию выключаем и в тот же прогон правим ей бюджет, цель и
    корректировки — все эти изменения не сделают ничего, но заплачены;
  • два действия на один и тот же сегмент в одном прогоне — второе затирает
    первое, а наблюдение потом судит их обоих по одному исходу.

Это и есть тот класс дефектов, ради которого нужен разбор беты: он не
воспроизводится в тестах рычага (рычаг прав) и не виден в журнале
применённого (обе строки применились штатно).

Разрешение конфликта здесь консервативное: сомнительную пару НЕ применяем.
Отложенное действие не потеряно — оно вернётся следующим прогоном, когда
рычаги придут к согласию; применённая же противоречивая пара стоит денег и
портит наблюдение обоим своим участникам.
"""

from typing import Any, Dict, Iterable, List, Optional, Tuple

# Виды из движка записи. Строками, а не импортом из writer: этот модуль
# читает УЖЕ СОБРАННОЕ действие и не должен зависеть от того, какой рычаг
# его собрал.
SUSPEND_TYPE = "CAMPAIGN_STATE"
BUDGET_TYPES = frozenset({"WEEKLY_SPEND_LIMIT", "DAILY_BUDGET"})
TCPA_TYPE = "AVERAGE_CPA"

# Поле, по которому у каждого вида читается величина: направление рычага
# выводится из самого действия (payload против previous_state), а не из
# отдельного флага, который рычаги должны были бы не забыть проставить.
AMOUNT_FIELDS = {
    "WEEKLY_SPEND_LIMIT": "WeeklySpendLimit",
    "AVERAGE_CPA": "TargetCpa",
}

# Блоки кабинета, которые действие ЗАМЕНЯЕТ целиком. Два действия на один
# блок одного объекта в одном такте — не спор рычагов, а затирание: оба
# собраны из ОДНОГО прочитанного состояния, и второе молча возвращает то, что
# поставило первое (стратегия и бюджет живут в BiddingStrategy вдвоём, к ним
# же относятся цель CPA и цель оптимизации). Наблюдение потом судит обоих по
# одному исходу.
#
# Читается по СОДЕРЖИМОМУ payload, а не по виду действия: модуль разбирает уже
# собранное действие и не должен знать, какой рычаг его собрал.
REPLACED_BLOCKS = ("BiddingStrategy", "DailyBudget", "TimeTargeting",
                   "NegativeKeywords", "ExcludedSites")

SUSPENDED_OBJECT = "conflict_suspended_object"
OPPOSING_LEVERS = "conflict_opposing_levers"
DUPLICATE_SEGMENT = "conflict_duplicate_segment"
SAME_BLOCK = "conflict_same_block"


def _amount(source: Any, field: str) -> Optional[float]:
    if not isinstance(source, dict):
        return None
    value = source.get(field)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def direction(action: Dict[str, Any]) -> int:
    """Куда рычаг двигает объект: +1 вверх, −1 вниз, 0 — неизвестно.

    Ноль возвращается честно: у части видов величины в действии нет (в
    payload уезжает готовая стратегия), и выдумывать направление нельзя —
    ложное «вверх» породило бы ложный конфликт, а он дороже пропущенного:
    из-за него встало бы законное изменение.
    """
    field = AMOUNT_FIELDS.get(str(action.get("direct_type")))
    if not field:
        return 0
    new = _amount(action.get("payload"), field)
    old = _amount(action.get("previous_state"), field)
    if new is None or old is None or new == old:
        return 0
    return 1 if new > old else -1


def _object(action: Dict[str, Any]) -> str:
    return f"{action.get('object_level')}:{action.get('object_id')}"


def _blocks(action: Dict[str, Any]) -> List[str]:
    payload = action.get("payload")
    if not isinstance(payload, dict):
        return []
    return [name for name in REPLACED_BLOCKS if name in payload]


def _segment(action: Dict[str, Any]) -> str:
    return f"{_object(action)}:{action.get('direct_type')}:{action.get('key')}"


def resolve(actions: Iterable[Dict[str, Any]]
            ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """План → (что применяем, что откладываем с причиной конфликта).

    Порядок разбора — от самого сильного противоречия к самому слабому:
    выключение объекта обесценивает всё остальное по нему; противонаправленная
    пара рычагов снимает обоих участников; дубликат сегмента оставляет первое
    действие и снимает последующие.

    Порядок действий на входе сохраняется: он уже отсортирован вызывающим по
    цене, и менять приоритет здесь — значит принимать решение, которое этому
    модулю не поручали.
    """
    plan = [a for a in actions if isinstance(a, dict)]
    dropped: List[Dict[str, Any]] = []

    suspended = {_object(a) for a in plan
                 if str(a.get("direct_type")) == SUSPEND_TYPE}
    kept: List[Dict[str, Any]] = []
    for action in plan:
        if (_object(action) in suspended
                and str(action.get("direct_type")) != SUSPEND_TYPE):
            dropped.append({**action, "conflict_reason": SUSPENDED_OBJECT})
            continue
        kept.append(action)

    # Противонаправленные рычаги: бюджет и цель конверсии на одном объекте.
    # Снимаются ОБА: который из них прав, здесь неизвестно, а применение
    # любого из пары оставляет второй рычаг при своём несогласии — то есть
    # следующий прогон повторит ту же пару.
    budget_dir: Dict[str, int] = {}
    tcpa_dir: Dict[str, int] = {}
    for action in kept:
        kind = str(action.get("direct_type"))
        move = direction(action)
        if not move:
            continue
        if kind in BUDGET_TYPES:
            budget_dir[_object(action)] = move
        elif kind == TCPA_TYPE:
            tcpa_dir[_object(action)] = move
    opposing = {obj for obj, move in budget_dir.items()
                if tcpa_dir.get(obj) not in (None, move)}

    survivors: List[Dict[str, Any]] = []
    for action in kept:
        kind = str(action.get("direct_type"))
        if _object(action) in opposing and (kind in BUDGET_TYPES or kind == TCPA_TYPE):
            dropped.append({**action, "conflict_reason": OPPOSING_LEVERS})
            continue
        survivors.append(action)

    # Затирание одного блока. Остаётся ПЕРВОЕ действие — вход уже отсортирован
    # вызывающим по цене, и снимать оба здесь незачем: они не спорят о
    # направлении, просто пишут в одно поле. Второе вернётся следующим тактом.
    single: List[Dict[str, Any]] = []
    taken: set = set()
    for action in survivors:
        blocks = [(_object(action), block) for block in _blocks(action)]
        if any(item in taken for item in blocks):
            dropped.append({**action, "conflict_reason": SAME_BLOCK})
            continue
        taken.update(blocks)
        single.append(action)

    final: List[Dict[str, Any]] = []
    seen: set = set()
    for action in single:
        segment = _segment(action)
        if segment in seen:
            dropped.append({**action, "conflict_reason": DUPLICATE_SEGMENT})
            continue
        seen.add(segment)
        final.append(action)
    return final, dropped


def by_reason(dropped: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for action in dropped or ():
        reason = str(action.get("conflict_reason") or "")
        out[reason] = out.get(reason, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
