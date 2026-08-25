# -*- coding: utf-8 -*-
"""
sync/agent/writer/risk.py — риск-бюджет движка записи (слой 4 защиты).

Идея из спеки: апрув — слабый предохранитель, потому что человек, которому
двадцать раз в неделю показывают «подтвердить?», через месяц штампует не глядя.
Вместо него — потолок денег под непроверенными изменениями.

Цена ошибки = деньги, которые ЭТО изменение ставит под удар, × дни до замера.
Обоими множителями управляем инженерно: первый считает exposure.py по каждому
рычагу отдельно, дни до замера — наша настройка. Худший случай посчитан заранее:
даже если КАЖДОЕ активное изменение окажется вредным, потери ограничены
недельным лимитом.

Прежде первым множителем был весь дневной расход кампании, одинаковый для
любого действия. Это завышало риск в разы и стоило темпа: одна корректировка
сегмента съедала 38 876 ₽ из 50 000 недельного лимита (кампания 114057545,
неделя 2026-08-17), агент делал одно-два действия в неделю, за всё время
применил девять. Разбор дефекта и правильная арифметика — в exposure.py.

Гарантия при этом не ослаблена, потому что рядом стоит ПОТОЛОК ОБЪЕКТА: сумма
дельт по кампании за окно не может превысить её расход за горизонт замера.
Больше, чем кампания тратит, на ней не потерять, сколько бы правок в неё ни
внесли; а внутри этого потолка каждое действие платит ровно свою дельту, а не
цену всей кампании.

Расход известен не для всех кампаний (лаг синка, новая кампания, пробел в
источнике). Молчаливый ноль для таких случаев — дыра в гарантии: нулевой риск
означает «бюджет не нужен», а на деле расход просто неизвестен. Различаем:
кампания есть в справочнике со значением 0 — риск честно 0 (тратить нечего);
кампании нет в справочнике — расход оценивается консервативно, по медиане
известных дневных расходов; справочник пуст целиком — оценить неоткуда,
риск = +inf, что гарантированно не проходит fit_into_budget и уходит в
отложенные, а не пропускается бесплатно.
"""

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sync.agent.writer import exposure as exposure_mod

DEFAULT_DAYS_TO_MEASURE = 7


def week_start(today_iso: str) -> str:
    d = datetime.fromisoformat(str(today_iso)).date() if not isinstance(today_iso, date) else today_iso
    return (d - timedelta(days=d.weekday())).isoformat()


DAYS_IN_WEEK = 7


def paced_allowance(remaining_rub: float, today_iso: str, week_start_iso: str,
                    days_in_week: int = DAYS_IN_WEEK) -> float:
    """Сколько недельного риска прогону позволено занять СЕГОДНЯ.

    Недельный лимит был потолком на неделю, но не на прогон: один прогон
    вправе был занять его целиком. С переходом на дельта-цены действия
    подешевели в разы, и лимит действий стал пропускать столько правок, что
    понедельничный прогон выбирал почти весь недельный риск, — а вторник и
    все дни после него оставались без бюджета. Это не безопасность, а
    случайность порядка сортировки: важное, замеченное в среду, ждало бы
    следующей недели за спиной у неважного, замеченного в понедельник.

    Деление на число ОСТАВШИХСЯ дней недели самокорректируется: неистраченное
    переходит вперёд (остаток тот же, делителей меньше), а в последний день
    недели доступен весь остаток. Потолок недели при этом не растёт — это
    по-прежнему он, просто выдаётся долями.
    """
    d = date.fromisoformat(str(today_iso))
    start = date.fromisoformat(str(week_start_iso))
    days_left = days_in_week - (d - start).days
    return float(remaining_rub) / max(1, min(days_in_week, days_left))


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


def object_daily_cost(
    action: Dict[str, Any],
    daily_cost_by_campaign: Dict[str, float],
) -> float:
    """Дневной расход объекта действия. +inf, если оценить не от чего.

    Кампания есть в справочнике — берём её расход как есть (в т.ч. честный
    0.0, если расхода нет). Кампании нет в справочнике — расход неизвестен,
    а не нулевой: берём консервативную оценку median_daily_cost по известным
    кампаниям. Если справочник пуст целиком — возвращаем +inf: действие с
    такой ценой fit_into_budget никогда не пропустит внутрь бюджета, оно
    уйдёт в отложенные вместо того, чтобы молча стоить 0.
    """
    object_id = str(action.get("object_id"))
    if object_id in daily_cost_by_campaign:
        return float(daily_cost_by_campaign[object_id])
    fallback = median_daily_cost(daily_cost_by_campaign)
    if fallback is None:
        return float("inf")
    return float(fallback)


def action_risk(
    action: Dict[str, Any],
    daily_cost_by_campaign: Dict[str, float],
    days_to_measure: int = DEFAULT_DAYS_TO_MEASURE,
) -> float:
    """Сколько денег максимум уйдёт неоптимально до момента замера.

    Дельта действия (exposure.daily_rub) × горизонт замера, но не больше
    расхода самого объекта за тот же горизонт: потерять на кампании больше,
    чем она тратит, нельзя — а рычаг бюджета «вверх» ограничен своим капом
    ±20 % от расхода, так что этот потолок ему не жмёт.
    """
    daily = object_daily_cost(action, daily_cost_by_campaign)
    if daily == float("inf"):
        return float("inf")
    at_risk, _ = exposure_mod.daily_rub(action.get("exposure"), daily)
    return round(min(at_risk, daily) * days_to_measure, 2)


def action_risk_basis(
    action: Dict[str, Any],
    daily_cost_by_campaign: Dict[str, float],
) -> str:
    """Почему цена действия такая — строкой для отчёта прогона.

    Без неё дельта-модель непроверяема: число в журнале не говорит, посчитана
    ли доля сегмента или молча взят весь объект.
    """
    daily = object_daily_cost(action, daily_cost_by_campaign)
    if daily == float("inf"):
        return "дневной расход неизвестен и оценить не от чего"
    _, basis = exposure_mod.daily_rub(action.get("exposure"), daily)
    return basis


def object_cap(
    action: Dict[str, Any],
    daily_cost_by_campaign: Dict[str, float],
    days_to_measure: int = DEFAULT_DAYS_TO_MEASURE,
) -> float:
    """Потолок риска объекта за окно: его расход за горизонт замера.

    Это то, что раньше платило КАЖДОЕ действие. Теперь это предел суммы всех
    действий по объекту: дельты складываются, пока не упрутся в цену объекта
    целиком, и дальше объект становится бесплатным — хуже, чем «вся кампания
    ошибочна», уже не будет.
    """
    daily = object_daily_cost(action, daily_cost_by_campaign)
    if daily == float("inf"):
        return float("inf")
    return round(daily * days_to_measure, 2)


def risk_object(action: Dict[str, Any]) -> str:
    """Единица риска — ОБЪЕКТ, на который действие влияет, а не само действие."""
    return f"{action.get('object_level')}:{action.get('object_id')}"


def fit_into_budget(
    actions: List[Dict[str, Any]],
    risks: Dict[str, float],
    remaining_rub: float,
    charged_by_object: Optional[Dict[str, float]] = None,
    caps: Optional[Dict[str, float]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Берёт действия по порядку, пока хватает бюджета. Остальное откладывает.

    Каждое действие платит свою дельту (risks), но сумма списаний по одному
    объекту ограничена его потолком (caps): дойдя до цены объекта целиком,
    следующие правки той же кампании проходят бесплатно. Прежде «бесплатным»
    было всё, кроме первого действия по объекту, а первое платило за всю
    кампанию — модель, при которой одно касание съедало три четверти
    недельного лимита.

    charged_by_object — сколько по объекту уже списано (журнал прошлых
    прогонов + этот прогон). Словарь ДОПОЛНЯЕТСЯ по ходу: вызывающий код
    передаёт один и тот же словарь на все кабинеты прогона, поэтому счёт по
    объекту сквозной, а не покабинетный.

    Списание происходит только при фактическом попадании в бюджет: действие,
    ушедшее в отложенные, счёт объекта не двигает — иначе следующее действие
    по тому же объекту прошло бы за счёт так и не применённого первого.

    Каждому действию в fits проставлен risk_rub — ровно та сумма, которую
    прогон за него заплатил. Она же уходит в журнал: по ней считается
    spent_risk следующего прогона.
    """
    charged: Dict[str, float] = charged_by_object if charged_by_object is not None else {}
    limits: Dict[str, float] = caps or {}
    fits: List[Dict[str, Any]] = []
    deferred: List[Dict[str, Any]] = []
    budget = float(remaining_rub)
    for action in actions:
        obj = risk_object(action)
        cap = float(limits.get(obj, float("inf")))
        already = float(charged.get(obj, 0.0))
        headroom = max(0.0, cap - already)
        cost = min(float(risks.get(action["idempotency_key"], 0.0)), headroom)
        if cost <= budget:
            fits.append({**action, "risk_rub": round(cost, 2)})
            budget -= cost
            charged[obj] = already + cost
        else:
            deferred.append(action)
    return fits, deferred
