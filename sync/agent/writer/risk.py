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
from typing import Any, Dict, List, Optional, Set, Tuple

DEFAULT_DAYS_TO_MEASURE = 7


def week_start(today_iso: str) -> str:
    d = datetime.fromisoformat(str(today_iso)).date() if not isinstance(today_iso, date) else today_iso
    return (d - timedelta(days=d.weekday())).isoformat()


def median(values) -> Optional[float]:
    """Медиана списка чисел. None, если список пуст — оценивать не от чего.

    Общий приём для консервативной оценки там, где для конкретного объекта
    нет собственных данных: половина известных объектов ниже медианы,
    половина — выше, оценка не занижена систематически в сторону нуля.
    Используется и для дневного расхода (median_daily_cost ниже), и для
    базового CPA красной линии (sync/agent_e1.py).
    """
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return None
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def median_daily_cost(daily_cost_by_campaign: Dict[str, float]) -> Optional[float]:
    """Медиана дневного расхода по известным кампаниям.

    Консервативная оценка для кампании без данных: половина известных
    кампаний тратит меньше, половина — больше, оценка не занижена
    систематически в сторону нуля. None, если справочник пуст —
    оценивать не от чего.
    """
    return median(daily_cost_by_campaign.values())


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


def risk_object(action: Dict[str, Any]) -> str:
    """Единица риска — ОБЪЕКТ, на который действие влияет, а не само действие."""
    return f"{action.get('object_level')}:{action.get('object_id')}"


def fit_into_budget(
    actions: List[Dict[str, Any]],
    risks: Dict[str, float],
    remaining_rub: float,
    charged_objects: Optional[Set[str]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Берёт действия по порядку, пока хватает бюджета. Остальное откладывает.

    Риск считается ПО ОБЪЕКТУ, а не по действию. Цена ошибки — дневной расход
    кампании × горизонт наблюдения; этот расход у кампании один, сколько бы
    корректировок ей ни ставили за прогон. Начисление каждому действию давало
    четырёхкратный расход на четыре корректировки одной кампании: модель
    расходилась с реальностью, бюджет выгорал на второй-третьей кампании, и
    посчитанные корректировки капали по паре в неделю.

    charged_objects — объекты, риск которых в этом прогоне уже списан.
    Множество ДОПОЛНЯЕТСЯ по ходу: вызывающий код передаёт одно и то же
    множество на все кабинеты прогона, поэтому «первое действие по объекту»
    определено на весь прогон, а не на один вызов.

    Списание происходит только при фактическом попадании в бюджет: действие,
    ушедшее в отложенные, объект не помечает — иначе следующее действие по
    тому же объекту прошло бы бесплатно за счёт так и не применённого первого.

    Каждому действию в fits проставлен risk_rub — ровно та сумма, которую
    прогон за него заплатил (полная цена объекта первому действию, 0 —
    остальным). Она же уходит в журнал: без этого spent_risk суммировал бы
    расход кампании столько раз, сколько по ней прошло действий.
    """
    charged: Set[str] = charged_objects if charged_objects is not None else set()
    fits: List[Dict[str, Any]] = []
    deferred: List[Dict[str, Any]] = []
    budget = float(remaining_rub)
    for action in actions:
        obj = risk_object(action)
        cost = 0.0 if obj in charged else float(risks.get(action["idempotency_key"], 0.0))
        if cost <= budget:
            fits.append({**action, "risk_rub": cost})
            budget -= cost
            charged.add(obj)
        else:
            deferred.append(action)
    return fits, deferred
