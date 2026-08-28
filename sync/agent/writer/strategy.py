# -*- coding: utf-8 -*-
"""
sync/agent/writer/strategy.py — Ф15 (запись): рычаг смены стратегии.

Стратегия — не настройка кампании рядом с прочими, а способ, которым Директ
принимает КАЖДОЕ решение о показе. Поэтому её смена стоит дороже любой другой
правки: обучение начинается с нуля, и до его конца кампания работает хуже, чем
работала.

Два перехода, и они несимметричны.

  «КЛИКИ → КОНВЕРСИИ» — покупка качества, и она законна только на накопленной
  статистике. Стратегия учится на достижениях цели; их нет — учиться не на чем,
  и «оптимизация конверсий» превращается в трату вслепую. Порог тот же, что у
  любого решения об объекте (power.MIN_EXPECTED_PAYMENTS), а живость цели
  проверяется тем же кодом, что у смены цели (writer/goal.py): цель, заведённая
  в счётчике, и цель, по которой приходят события, — разные вещи, и цена этой
  разницы измерена дважды (LIME 15.06, EDU ML-цели без тегов).

  «КОНВЕРСИИ → КЛИКИ» — не выбор, а СПАСЕНИЕ. Развернуть работающую
  конверсионную стратегию в клики агенту не на чем: это отказ от оптимизации в
  пользу объёма, решение продуктовое, а не арифметическое. Законен переход
  ровно тогда, когда цель, на которой стратегия училась, перестала приходить:
  тогда кампания уже работает вслепую, и клики честнее слепых конверсий.

ДЕНЬГИ ПРИ ПЕРЕХОДЕ. Ограничитель расхода живёт в разных местах: у
конверсионных стратегий — WeeklySpendLimit ВНУТРИ блока стратегии, у ручных —
DailyBudget самой кампании (writer/budget.py). Переход уносит ограничитель
вместе со стратегией, поэтому оба направления требуют, чтобы на той стороне
ограничитель был: иначе кампания на несколько часов остаётся без лимита вовсе.

ФОРМА ЗАПРОСА НЕ ВЫДУМЫВАЕТСЯ. Имя подблока параметров выводится из типа
стратегии, и ошибка в нём — не «поле проигнорировано», а отказ API целиком.
Справочник STRATEGY_FORMS закрыт и собран по тому, что реально читается из
кабинета (sync/edu_direct_settings.py); тип за его пределами — отказ, а не
догадка по созвучию имени.
"""

import copy
import hashlib
from typing import Any, Dict, List, Optional, Tuple

from sync.agent.writer import exposure, expectation, goal

MICROS = 1_000_000

STRATEGY_KIND = "strategy.set"

# На чём стратегия учится. Различение нужно не для красоты: от него зависит,
# какое основание требуется для перехода и где лежит ограничитель расхода.
LEARNS_ON_CLICKS = "clicks"
LEARNS_ON_CONVERSIONS = "conversions"

# Закрытый справочник форм: тип стратегии → имя подблока параметров и то, на
# чём она учится. Пусто в "block" — у формы своего подблока нет вовсе
# (проверено на кабинете: HIGHEST_POSITION приходит одним полем типа).
STRATEGY_FORMS: Dict[str, Dict[str, Any]] = {
    "HIGHEST_POSITION": {"block": None, "learns_on": LEARNS_ON_CLICKS},
    "AVERAGE_CPA": {"block": "AverageCpa", "learns_on": LEARNS_ON_CONVERSIONS},
}

UNKNOWN_FORM_REASON = (
    "тип стратегии {type} вне справочника форм: имя подблока параметров "
    "выводится из типа, и догадка означала бы отказ API целиком, а не "
    "«поле проигнорировано»"
)
NO_WEEKLY_LIMIT_REASON = (
    "недельный лимит для конверсионной стратегии не задан: у ручной деньги "
    "держит дневной бюджет, у конверсионной — WeeklySpendLimit внутри "
    "стратегии, и переход без него оставил бы кампанию без ограничения расхода"
)
NO_TARGET_CPA_REASON = (
    "цель CPA для конверсионной стратегии не задана: стратегии нечем "
    "ограничивать цену конверсии"
)
NO_DAILY_BUDGET_REASON = (
    "у кампании нет дневного бюджета: вместе с конверсионной стратегией "
    "уходит недельный лимит, и кампания осталась бы без ограничения расхода"
)
GOAL_STILL_WORKS_REASON = (
    "цель {goal} работает ({reaches} достижений за {days} дн.): разворот "
    "работающей конверсионной стратегии в клики — отказ от оптимизации в "
    "пользу объёма, и основания для него у агента нет"
)
PACKAGE_REASON = (
    "кампания в пакетной стратегии {strategy_id}: стратегия задаётся на "
    "пакет, правка одной кампании тронула бы соседние"
)
NOT_TEXT_REASON = "не текстовая кампания: структура стратегии другая"


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def read_strategy_type(strategy: Dict[str, Any],
                       channel: str = "Search") -> Optional[str]:
    """Тип стратегии канала, как он прочитан из кабинета."""
    block = (strategy or {}).get(channel)
    if not isinstance(block, dict):
        return None
    value = block.get("BiddingStrategyType")
    return str(value) if value else None


def _form_of(strategy_type: Optional[str]) -> Optional[Dict[str, Any]]:
    return STRATEGY_FORMS.get(str(strategy_type))


def strategy_with_type(strategy: Dict[str, Any], strategy_type: str,
                       params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Копия блока с новой стратегией поиска. Сетевой канал — как прочитан.

    Подблок ПРЕЖНЕЙ стратегии убирается: оставленный AverageCpa в ручной
    стратегии — противоречивое тело запроса, и Директ отвечает на него отказом
    уровня элемента, то есть молча в успешном HTTP-ответе.
    """
    out = copy.deepcopy(strategy)
    search = {"BiddingStrategyType": str(strategy_type)}
    form = _form_of(strategy_type)
    block = (form or {}).get("block")
    if block:
        search[block] = params or {}
    out["Search"] = search
    return out


def _params_for(strategy_type: str, move: Dict[str, Any]) -> Dict[str, Any]:
    """Параметры новой стратегии в единицах API (микрорубли)."""
    if str(strategy_type) != "AVERAGE_CPA":
        return {}
    params: Dict[str, Any] = {
        "AverageCpa": int(round(float(move["target_cpa"]))) * MICROS,
        "WeeklySpendLimit": int(round(float(move["weekly_limit"]))) * MICROS,
    }
    goal_ids = [int(g) for g in (move.get("goal_ids") or [])]
    if goal_ids:
        params["PriorityGoals"] = [{"GoalId": g} for g in goal_ids]
    return params


def _idempotency_key(campaign_id: str, strategy_type: str,
                     params: Dict[str, Any]) -> str:
    goals = ",".join(str(item.get("GoalId"))
                     for item in params.get("PriorityGoals") or [])
    raw = (f"strategy:{campaign_id}:{strategy_type}:"
           f"{params.get('AverageCpa')}:{params.get('WeeklySpendLimit')}:{goals}")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _to_conversions_refusal(move: Dict[str, Any]) -> Optional[str]:
    """Чего не хватает для перехода на конверсионную стратегию."""
    goal_ids = [int(g) for g in (move.get("goal_ids") or [])]
    if not goal_ids:
        return goal.UNKNOWN_REACHES_REASON.format(goal="—")
    reason = goal.liveness_refusal(goal_ids, move)
    if reason:
        return reason
    if _number(move.get("target_cpa")) is None:
        return NO_TARGET_CPA_REASON
    if _number(move.get("weekly_limit")) is None:
        return NO_WEEKLY_LIMIT_REASON
    return None


def _to_clicks_refusal(move: Dict[str, Any],
                       state: Dict[str, Any]) -> Optional[str]:
    """Чего не хватает для разворота конверсионной стратегии в клики."""
    goal_ids = [int(g) for g in (move.get("goal_ids") or [])]
    reaches = move.get("reaches") or {}
    days = int(_number(move.get("window_days")) or goal.DEFAULT_WINDOW_DAYS)
    for goal_id in goal_ids:
        value = _number(reaches.get(goal_id, reaches.get(str(goal_id))))
        if value is None:
            return goal.UNKNOWN_REACHES_REASON.format(goal=goal_id)
        if value > 0:
            return GOAL_STILL_WORKS_REASON.format(goal=goal_id, days=days,
                                                  reaches=int(value))
    if not state.get("daily_budget"):
        return NO_DAILY_BUDGET_REASON
    return None


def diff_strategy(
    desired: Dict[str, Dict[str, Any]],
    actual_by_campaign: Dict[str, Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Желаемые стратегии × прочитанное состояние кабинета → (действия, отказы).

    desired — по кампании: strategy_type (куда переводим), цель и её
    достижения за окно, деньги новой стратегии (target_cpa, weekly_limit) и
    экономика обещания (клики в день, две конверсии).

    Кампании без записи в actual_by_campaign не порождают ни действия, ни
    отказа: их не оказалось в кабинете, и это видно счётчиком not_found
    вызывающего.
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

        target_type = str(move.get("strategy_type") or "")
        target_form = _form_of(target_type)
        if target_form is None:
            refused.append({"campaign_id": cid,
                            "reason": UNKNOWN_FORM_REASON.format(type=target_type)})
            continue

        strategy = state.get("strategy") if isinstance(state.get("strategy"), dict) else {}
        current_type = read_strategy_type(strategy)
        if _form_of(current_type) is None:
            refused.append({"campaign_id": cid,
                            "reason": UNKNOWN_FORM_REASON.format(type=current_type)})
            continue
        if current_type == target_type:
            continue

        if target_form["learns_on"] == LEARNS_ON_CONVERSIONS:
            reason = _to_conversions_refusal(move)
        else:
            reason = _to_clicks_refusal(move, state)
        if reason:
            refused.append({"campaign_id": cid, "reason": reason})
            continue

        params = _params_for(target_type, move)
        actions.append(expectation.attach({
            "action_kind": STRATEGY_KIND,
            "object_level": "campaign",
            "object_id": str(cid),
            "exposure": exposure.whole_object_exposure(
                "смена стратегии перезапускает обучение с нуля: до конца "
                "замера весь расход кампании идёт по новому правилу"),
            "key": "bidding_strategy",
            "payload": {
                "CampaignId": int(cid),
                "BiddingStrategy": strategy_with_type(strategy, target_type, params),
                "BiddingStrategyType": target_type,
            },
            "previous_state": {
                # Тип И параметры: тип без параметров вернул бы кампанию в
                # стратегию с чужими деньгами, параметры без типа — никуда.
                "BiddingStrategy": strategy,
                "BiddingStrategyTypes": {
                    "Search": current_type,
                    "Network": read_strategy_type(strategy, "Network"),
                },
            },
            "idempotency_key": _idempotency_key(str(cid), target_type, params),
        }, _expectation_context(move)))
    return actions, refused


def _expectation_context(move: Dict[str, Any]) -> Dict[str, Any]:
    """Вход обещания: клики в день и две конверсии — текущая и ожидаемая.

    Конверсия под новой стратегией у этой кампании не наблюдалась ни дня и
    перенесена с соседнего объекта — ровно это делает действие ставкой, а не
    измерением (writer/tier.TRANSFERRED_EVIDENCE_KINDS).
    """
    return {"clicks_per_day": move.get("clicks_per_day"),
            "cr_current": move.get("cr_current"),
            "cr_new": move.get("cr_new")}


def to_api_call(action: Dict[str, Any]) -> Tuple[str, str, Dict[str, Any]]:
    """Действие → вызов API. Тонкая обёртка над общим сборщиком apply."""
    from sync.agent.writer.apply import to_api_call as apply_to_api_call
    return apply_to_api_call(action)
