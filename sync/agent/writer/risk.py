# -*- coding: utf-8 -*-
"""
sync/agent/writer/risk.py — риск-бюджет движка записи (слой 4 защиты).

Идея из спеки: апрув — слабый предохранитель, потому что человек, которому
двадцать раз в неделю показывают «подтвердить?», через месяц штампует не глядя.
Вместо него — потолок денег под непроверенными изменениями.

Цена ошибки = дневной расход объекта × дни до обнаружения. Обоими множителями
управляем инженерно: расход известен из фактов, дни до замера — наша настройка.
Худший случай посчитан заранее: даже если КАЖДОЕ активное изменение окажется
вредным, потери ограничены недельным лимитом.
"""

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Tuple

DEFAULT_DAYS_TO_MEASURE = 7


def week_start(today_iso: str) -> str:
    d = datetime.fromisoformat(str(today_iso)).date() if not isinstance(today_iso, date) else today_iso
    return (d - timedelta(days=d.weekday())).isoformat()


def action_risk(
    action: Dict[str, Any],
    daily_cost_by_campaign: Dict[str, float],
    days_to_measure: int = DEFAULT_DAYS_TO_MEASURE,
) -> float:
    """Сколько денег максимум уйдёт неоптимально до момента замера."""
    daily = float(daily_cost_by_campaign.get(str(action.get("object_id")), 0.0))
    return round(daily * days_to_measure, 2)


def fit_into_budget(
    actions: List[Dict[str, Any]], risks: Dict[str, float], remaining_rub: float
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Берёт действия по порядку, пока хватает бюджета. Остальное откладывает."""
    fits: List[Dict[str, Any]] = []
    deferred: List[Dict[str, Any]] = []
    budget = float(remaining_rub)
    for action in actions:
        cost = float(risks.get(action["idempotency_key"], 0.0))
        if cost <= budget:
            fits.append(action)
            budget -= cost
        else:
            deferred.append(action)
    return fits, deferred
