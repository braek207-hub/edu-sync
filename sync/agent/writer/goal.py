# -*- coding: utf-8 -*-
"""
sync/agent/writer/goal.py — Ф15 (запись): рычаг смены цели оптимизации.

Цель оптимизации — то, ЧЕМУ учится стратегия. Это не «ещё одна настройка
рядом с бюджетом»: все решения автостратегии (кому показать, сколько
заплатить за клик) выводятся из того, какое событие она считает успехом.

Отсюда единственная проверка, ради которой рычаг и написан: ЖИВА ЛИ ЦЕЛЬ.
Не «заведена ли она в счётчике» — заведена бывает и мёртвая, — а приходят ли
по ней достижения. Цена ошибки измерена дважды:

  * LIME: цель 1900016999 сломалась 15.06.2026, и смарт-кампании крутились
    вслепую до самого аудита в августе (память lime-direct-drr-audit-2026-08);
  * EDU: ML-цели 593523067 и 593523237 заведены в Метрике, но событий у них
    ноль — теги ЯТМ не вставлены (аудит Директа 19.08.2026,
    docs/AGENT-DATA-SOURCES.md).

Обе — цели, которые выглядят пригодными в любом справочнике целей и по
которым автостратегия не получит ни одного сигнала.

Три отличия от соседних рычагов, и все содержательные:

  * ПОД УДАРОМ ОБЪЕКТ ЦЕЛИКОМ. У корректировки под ударом доля сегмента, у
    цели CPA — относительный сдвиг ручки. Здесь стратегия переучивается вся,
    и весь расход кампании до конца замера идёт по новому правилу
    (exposure.whole_object_exposure).
  * ЭТО СТАВКА, А НЕ ИЗМЕРЕНИЕ. Истории по новой цели у этой кампании нет по
    построению: конверсия, из которой посчитано обещание, снята с соседнего
    объекта. Класс достоверности — 2 (writer/tier.TRANSFERRED_EVIDENCE_KINDS).
  * ОБУЧЕНИЕ СБРАСЫВАЕТСЯ. Справка Директа относит корректировку целевых
    действий к тому же классу, что смену стратегии, поэтому вид стоит в
    learning.RESETS_LEARNING и запирается кулдауном обучения.

Блок BiddingStrategy уходит в update ЦЕЛИКОМ, как прочитан, с заменой одного
поля цели: структура в API заменяется, а не сливается по полям, и пересборка
потеряла бы соседние настройки (недельный лимит, цель CPA, потолок ставки),
которые ставил человек. Тот же довод, что у writer/budget.py и writer/tcpa.py.
"""

import copy
import hashlib
from typing import Any, Dict, List, Optional, Tuple

from sync.agent import power
from sync.agent.writer import exposure, expectation

GOAL_KIND = "goal.set"

# Сколько достижений за окно нужно, чтобы на цели вообще можно было учиться.
# Порог НЕ свой: столько же требует любое решение об объекте (power.py), и две
# копии одного числа разъехались бы на первой правке одной из них.
MIN_GOAL_REACHES = power.MIN_EXPECTED_PAYMENTS

# Окно, за которое считаются достижения, если вызывающий его не назвал.
# Месяц: цель с редким, но регулярным событием (оплата) на недельном окне
# выглядела бы мёртвой ровно так же, как цель без тега.
DEFAULT_WINDOW_DAYS = 28

NO_EVENTS_REASON = (
    "по цели {goal} за {days} дн. не пришло ни одного достижения — событие "
    "не приходит: тега на сайте нет или он сломан. Перевод стратегии на "
    "такую цель ослепляет кампанию целиком"
)
THIN_REASON = (
    "у цели {goal} за {days} дн. всего {reaches} достижений — мало, чтобы "
    "стратегия на ней научилась (нужно от {minimum})"
)
UNKNOWN_REACHES_REASON = (
    "число достижений цели {goal} неизвестно: молчание о событиях — не «их "
    "много», а отсутствие основания вообще"
)
PACKAGE_REASON = (
    "кампания в пакетной стратегии {strategy_id}: цель задаётся на пакет, "
    "правка одной кампании тронула бы соседние"
)
NOT_TEXT_REASON = "не текстовая кампания: структура стратегии другая"
PLACEHOLDER_REASON = (
    "в стратегии кампании стоит GoalId 13 — не цель, а признак «цели заданы "
    "на уровне кампании» (PriorityGoals самой кампании). Записать сюда "
    "настоящую цель значило бы подменить признак значением"
)
NO_HOLDER_REASON = (
    "у стратегии кампании нет носителя цели оптимизации — ручной стратегии "
    "цель не назначается вовсе, и «поставить» её значило бы создать поле, "
    "которого у объекта нет"
)
PAID_VALUE_REASON = (
    "цель кампании несёт ЦЕНУ конверсии (оплата за конверсию): ценность "
    "нового события неизвестна, а перенести прежнюю значило бы объявить два "
    "разных события равными по деньгам"
)
MANY_INTO_ONE_REASON = (
    "носитель цели у стратегии один (GoalId), а предложено целей {count}: "
    "форма запроса подменой списка не собирается"
)


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None   # NaN — то же «неизвестно»


# GoalId 13 — не цель, а placeholder API Директа: «цели заданы приоритетными
# целями кампании». Число не наше, оно прочитано у читателя кабинета
# (sync/edu_direct_settings._goal_ids_from_block), где по нему же и
# отбрасывается.
PLACEHOLDER_GOAL_ID = 13


def _as_list(value: Any) -> List[Any]:
    """Список приоритетных целей из блока. Форма — как у читателя кабинета.

    Директ отдаёт PriorityGoals то массивом, то обёрткой {"Items": [...]}, а
    элементы — то словарями, то голыми числами
    (sync/edu_direct_settings._as_list / _priority_goal_ids_from_block).
    Своя, более узкая форма означала бы, что рычаг читает пусто там, где
    читатель кабинета читает цели, — и отказывался бы с неверной причиной.
    """
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        items = value.get("Items")
        if isinstance(items, list):
            return items
    return []


def _goal_holder(block: Any) -> Optional[Dict[str, Any]]:
    """Вложенный словарь блока канала, несущий цель оптимизации.

    Поиск по СОДЕРЖИМОМУ, а не по имени подблока, — тот же довод, что у
    tcpa._target_holder: имя подблока выводится из типа стратегии, справочник
    имён пришлось бы править на каждый новый тип, а забытая правка означала бы
    молчаливый пропуск. Носителей два по форме: список приоритетных целей и
    одиночный GoalId у стратегий вида «максимум конверсий».
    """
    if not isinstance(block, dict):
        return None
    for value in block.values():
        if isinstance(value, dict) and ("PriorityGoals" in value
                                        or "GoalId" in value):
            return value
    return None


def _holder_of(strategy: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for channel in ("Search", "Network"):
        holder = _goal_holder((strategy or {}).get(channel))
        if holder is not None:
            return holder
    return None


def read_goal_ids(strategy: Dict[str, Any]) -> List[int]:
    """Цели оптимизации, стоящие в блоке BiddingStrategy. Пусто — их нет."""
    holder = _holder_of(strategy)
    if holder is None:
        return []
    if "PriorityGoals" in holder:
        ids = []
        for item in _as_list(holder.get("PriorityGoals")):
            value = (_number(item.get("GoalId")) if isinstance(item, dict)
                     else _number(item))
            if value is not None and int(value) != PLACEHOLDER_GOAL_ID:
                ids.append(int(value))
        return ids
    value = _number(holder.get("GoalId"))
    if value is None or int(value) == PLACEHOLDER_GOAL_ID:
        return []
    return [int(value)]


def has_conversion_value(strategy: Dict[str, Any]) -> bool:
    """Несёт ли цель кампании цену конверсии (оплата за конверсию)."""
    holder = _holder_of(strategy)
    if holder is None:
        return False
    return any(isinstance(item, dict) and item.get("Value") is not None
               for item in _as_list(holder.get("PriorityGoals")))


def _is_placeholder(holder: Dict[str, Any]) -> bool:
    """Стоит ли в носителе placeholder вместо цели.

    Отличать это состояние обязательно: цели такой кампании живут не в
    стратегии, а в самой кампании (TextCampaign.PriorityGoals), и запись в
    стратегию их не тронет — зато затрёт признак.
    """
    value = _number(holder.get("GoalId"))
    return value is not None and int(value) == PLACEHOLDER_GOAL_ID


def strategy_with_goals(strategy: Dict[str, Any],
                        goal_ids: List[int]) -> Dict[str, Any]:
    """Копия блока BiddingStrategy с новыми целями оптимизации.

    Пишем в тот носитель, который ПРОЧИТАН: подмена формы (список вместо
    одиночного GoalId и наоборот) — отказ API целиком, а не «поле
    проигнорировано».
    """
    out = copy.deepcopy(strategy)
    holder = _holder_of(out)
    if holder is None:
        raise ValueError("в стратегии нет носителя цели оптимизации")
    if "PriorityGoals" in holder:
        holder["PriorityGoals"] = [{"GoalId": int(g)} for g in goal_ids]
        return out
    if len(goal_ids) != 1:
        raise ValueError(MANY_INTO_ONE_REASON.format(count=len(goal_ids)))
    holder["GoalId"] = int(goal_ids[0])
    return out


def _idempotency_key(campaign_id: str, goal_ids: List[int]) -> str:
    # Порядок целей в ключ не входит: список «А, Б» и список «Б, А» — одно и
    # то же состояние кабинета, и разные ключи означали бы второе применение
    # того же изменения.
    raw = f"goal:{campaign_id}:{','.join(str(g) for g in sorted(goal_ids))}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _reach_of(reaches: Dict[Any, Any], goal_id: int) -> Optional[float]:
    """Достижения цели. Ключ ищется и числом, и строкой: словарь приходит из
    JSON-подобных источников, где целые ключи становятся строками."""
    if goal_id in reaches:
        return _number(reaches[goal_id])
    return _number(reaches.get(str(goal_id)))


def liveness_refusal(goal_ids: List[int], move: Dict[str, Any]
                     ) -> Optional[str]:
    """Причина, по которой на предложенные цели переходить нельзя, или None.

    Публичная: ту же проверку делает рычаг смены стратегии (writer/strategy),
    и вторая копия «жива ли цель» разошлась бы с этой на первой же правке —
    а расходиться ей нельзя, обе решают один вопрос об одном кабинете.
    """
    reaches = move.get("reaches") or {}
    days = int(_number(move.get("window_days")) or DEFAULT_WINDOW_DAYS)
    for goal_id in goal_ids:
        value = _reach_of(reaches, goal_id)
        if value is None:
            return UNKNOWN_REACHES_REASON.format(goal=goal_id)
        if value <= 0:
            return NO_EVENTS_REASON.format(goal=goal_id, days=days)
        if value < MIN_GOAL_REACHES:
            return THIN_REASON.format(goal=goal_id, days=days,
                                      reaches=int(value),
                                      minimum=int(MIN_GOAL_REACHES))
    return None


def diff_goal(
    desired: Dict[str, Dict[str, Any]],
    actual_by_campaign: Dict[str, Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Желаемые цели × прочитанное состояние кабинета → (действия, отказы).

    desired — по кампании: goal_ids (куда переводим), reaches (достижения
    каждой предложенной цели за окно) и экономика обещания — клики в день и
    две конверсии, текущей цели и новой.

    actual_by_campaign — прочитанное состояние (budget.fetch_budget_state):
    блок BiddingStrategy оттуда уже свежий, и второй поход в API за тем же был
    бы лишним. Кампании без записи в нём не порождают ни действия, ни отказа —
    их не оказалось в кабинете, и это видно счётчиком not_found вызывающего.
    """
    actions: List[Dict[str, Any]] = []
    refused: List[Dict[str, Any]] = []

    for cid in sorted(desired):
        move = desired[cid]
        state = actual_by_campaign.get(str(cid))
        if state is None:
            continue
        if state.get("package_id"):
            refused.append({"campaign_id": cid,
                            "reason": PACKAGE_REASON.format(
                                strategy_id=state["package_id"])})
            continue
        if state.get("campaign_type") not in (None, "TEXT_CAMPAIGN"):
            refused.append({"campaign_id": cid, "reason": NOT_TEXT_REASON})
            continue

        goal_ids = [int(g) for g in (move.get("goal_ids") or [])]
        if not goal_ids:
            continue

        reason = liveness_refusal(goal_ids, move)
        if reason:
            refused.append({"campaign_id": cid, "reason": reason})
            continue

        strategy = state.get("strategy")
        holder = _holder_of(strategy) if isinstance(strategy, dict) else None
        if holder is None:
            refused.append({"campaign_id": cid, "reason": NO_HOLDER_REASON})
            continue
        if _is_placeholder(holder):
            refused.append({"campaign_id": cid, "reason": PLACEHOLDER_REASON})
            continue
        current_ids = read_goal_ids(strategy)
        if has_conversion_value(strategy):
            refused.append({"campaign_id": cid, "reason": PAID_VALUE_REASON})
            continue
        if sorted(current_ids) == sorted(goal_ids):
            continue

        try:
            payload_strategy = strategy_with_goals(strategy, goal_ids)
        except ValueError:
            refused.append({"campaign_id": cid,
                            "reason": MANY_INTO_ONE_REASON.format(
                                count=len(goal_ids))})
            continue

        actions.append(expectation.attach({
            "action_kind": GOAL_KIND,
            "object_level": "campaign",
            "object_id": str(cid),
            "exposure": exposure.whole_object_exposure(
                "смена цели перезапускает обучение стратегии: до конца замера "
                "весь расход кампании идёт по новому правилу"),
            "key": "optimization_goal",
            "payload": {
                "CampaignId": int(cid),
                "BiddingStrategy": payload_strategy,
                "GoalIds": goal_ids,
            },
            "previous_state": {
                "BiddingStrategy": strategy,
                "GoalIds": current_ids,
            },
            "idempotency_key": _idempotency_key(str(cid), goal_ids),
        }, _expectation_context(move)))
    return actions, refused


def _expectation_context(move: Dict[str, Any]) -> Dict[str, Any]:
    """Вход обещания: клики в день и две конверсии — текущей цели и новой.

    Курс «клики → лиды» не выдумывается и не берётся из средней по кабинету:
    без конверсии НОВОЙ цели обещание было бы прогнозом из воздуха, а петля
    обучения зачла бы его сбывшимся по любому исходу.
    """
    return {"clicks_per_day": move.get("clicks_per_day"),
            "cr_current": move.get("cr_current"),
            "cr_new": move.get("cr_new")}


def to_api_call(action: Dict[str, Any]) -> Tuple[str, str, Dict[str, Any]]:
    """Действие → вызов API. Тонкая обёртка над общим сборщиком apply."""
    from sync.agent.writer.apply import to_api_call as apply_to_api_call
    return apply_to_api_call(action)
