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

Расход известен не для всех кампаний (лаг синка, новая кампания, пробел в
источнике). Молчаливый ноль для таких случаев — дыра в гарантии: нулевой риск
означает «бюджет не нужен», а на деле расход просто неизвестен. Различаем:
кампания есть в справочнике со значением 0 — риск честно 0 (тратить нечего);
кампании нет в справочнике — риск оценивается консервативно, по медиане
известных дневных расходов; справочник пуст целиком — оценить неоткуда,
риск = +inf, что гарантированно не проходит fit_into_budget и уходит в
отложенные, а не пропускается бесплатно.
"""

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_DAYS_TO_MEASURE = 7


def week_start(today_iso: str) -> str:
    d = datetime.fromisoformat(str(today_iso)).date() if not isinstance(today_iso, date) else today_iso
    return (d - timedelta(days=d.weekday())).isoformat()


def median_daily_cost(daily_cost_by_campaign: Dict[str, float]) -> Optional[float]:
    """Медиана дневного расхода по известным кампаниям.

    Консервативная оценка для кампании без данных: половина известных
    кампаний тратит меньше, половина — больше, оценка не занижена
    систематически в сторону нуля. None, если справочник пуст —
    оценивать не от чего.
    """
    values = sorted(float(v) for v in daily_cost_by_campaign.values())
    if not values:
        return None
    n = len(values)
    mid = n // 2
    if n % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2.0


def action_risk(
    action: Dict[str, Any],
    daily_cost_by_campaign: Dict[str, float],
    days_to_measure: int = DEFAULT_DAYS_TO_MEASURE,
) -> float:
    """Сколько денег максимум уйдёт неоптимально до момента замера.

    Кампания есть в справочнике — берём её расход как есть (в т.ч. честный
    0.0, если расхода нет). Кампании нет в справочнике — расход неизвестен,
    а не нулевой: берём консервативную оценку median_daily_cost по известным
    кампаниям. Если справочник пуст целиком — оценить неоткуда, возвращаем
    +inf: действие с такой ценой fit_into_budget никогда не пропустит внутрь
    бюджета, оно уйдёт в отложенные вместо того, чтобы молча стоить 0.
    """
    object_id = str(action.get("object_id"))
    if object_id in daily_cost_by_campaign:
        daily = float(daily_cost_by_campaign[object_id])
    else:
        fallback = median_daily_cost(daily_cost_by_campaign)
        if fallback is None:
            return float("inf")
        daily = fallback
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
