# -*- coding: utf-8 -*-
"""
sync/agent/ladder.py — лестница воронки (Э2.1): выбор рабочей метрики объекта.

Прежний порог решений был один — Σ p_pay ≥ 25 за окно — и его не проходила НИ ОДНА
из 83 кампаний (power.pass_decision_threshold = 0). Механизм формально существовал,
а решений не принимал. Лестница обобщает тот же статистический смысл на всю воронку:

    оплаты → сделки → соединения → эффективные лиды → лиды → клики

Объект получает САМУЮ ГЛУБОКУЮ ступень, где у него ≥ MIN_STEP_EVENTS событий за
окно, и её значение пересчитывается в ожидаемые оплаты через исторические
коэффициенты перехода. Порог 25 событий — это относительная ошибка счётчика
1/√25 = 20 % (пуассоновский счёт); прежние «25 ожидаемых оплат» несли ровно тот же
смысл, но требовали его от самой редкой ступени.

Коэффициенты перехода берутся НЕ у объекта (у него событий глубоких ступеней как
раз мало), а у пула похожих — направление, затем кабинет (Э2.0 показал, что
p_pay-суррогат на уровне направлений хуже наивной оценки, поэтому наивная оценка
через воронку пула — и есть правильный инструмент). Коэффициент надёжен, когда в
его ЧИСЛИТЕЛЕ у пула ≥ MIN_RATE_EVENTS событий; иначе берётся самый общий пул
с пометкой weak — решение по слабому коэффициенту дороже, и слой уверенности
(Э2.3) обязан это видеть.

Итоговая относительная ошибка оценки складывается из ошибки ступени объекта и
ошибок использованных коэффициентов: √(1/n_ступени + Σ 1/n_числителя_коэффициента).
Она печатается рядом с оценкой — без неё «ожидаемые оплаты 12.7» читаются как
факт, а не как оценка с точностью ±30 %.

Модуль чистый: ни БД, ни дат — только счётчики, которые собрал вызывающий.
"""

from math import sqrt
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Глубокая → мелкая. Ключи совпадают с полями витрины фактов (facts.py):
# paid=payments_fact, deals=deals, connected=connected_leads, eff=eff_leads.
STEP_ORDER: Tuple[str, ...] = ("paid", "deals", "connected", "eff", "leads", "clicks")

# Пары (числитель, знаменатель) коэффициентов перехода, от глубины к поверхности.
RATE_PAIRS: Tuple[Tuple[str, str], ...] = (
    ("paid", "deals"),
    ("deals", "connected"),
    ("connected", "eff"),
    ("eff", "leads"),
    ("leads", "clicks"),
)

# 1/√25 = 20 % относительной ошибки счётчика — смысл прежнего порога Σp_pay ≥ 25,
# перенесённый на любую ступень.
MIN_STEP_EVENTS = 25.0
MIN_RATE_EVENTS = 25.0

NO_STEP_REASON = (
    f"ни одна ступень воронки не набрала {int(MIN_STEP_EVENTS)} событий за окно — "
    "объекту нечем меряться, решения только на пуле похожих"
)


def _count(counts: Dict[str, Any], step: str) -> float:
    return float(counts.get(step) or 0.0)


def choose_step(counts: Dict[str, Any]) -> Optional[str]:
    """Самая глубокая ступень с достаточным числом событий."""
    for step in STEP_ORDER:
        if _count(counts, step) >= MIN_STEP_EVENTS:
            return step
    return None


def transition_rate(
    num: str, den: str, pools: Sequence[Tuple[str, Dict[str, Any]]],
    own_counts: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Коэффициент перехода den → num из первого пула, где он надёжен.

    Надёжность — по событиям в ЧИСЛИТЕЛЕ: ошибка доли определяется редким
    событием, а не объёмом знаменателя. Ни один пул не надёжен — берётся самый
    общий с weak=True. Совсем нет данных (знаменатель нулевой везде) — None:
    пересчёт через эту пару честно невозможен.
    """
    fallback = None
    for name, counts in pools:
        n_num, n_den = _count(counts, num), _count(counts, den)
        if n_den <= 0:
            continue
        candidate = {
            "num": num, "den": den,
            "rate": n_num / n_den,
            "num_events": n_num,
            "source": name,
            "weak": n_num < MIN_RATE_EVENTS,
        }
        if not candidate["weak"]:
            return _with_own(candidate, num, den, own_counts)
        fallback = candidate  # пулы идут от частного к общему — берём последний
    return _with_own(fallback, num, den, own_counts) if fallback else None


def _with_own(pool_rate: Dict[str, Any], num: str, den: str,
              own_counts: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Пуловый коэффициент, подтянутый к собственным данным объекта.

    Объект на мелкой ступени получал коэффициент пула ЦЕЛИКОМ: мусорная
    кампания с дешёвым кликом внутри направления выглядела средней и
    доливалась (аудит 2026-08-23, разброс выручки на лид внутри направления
    117–5831 ₽). Своих событий у неё для самостоятельной оценки не хватает —
    иначе лестница выбрала бы более глубокую ступень, — но они не ноль и
    обязаны тянуть коэффициент к себе.

    Вес собственных данных — n/(n + MIN_RATE_EVENTS): приор ровно в тот
    объём событий, начиная с которого коэффициент считается надёжным сам по
    себе. Свой знаменатель нулевой — веса нет, коэффициент остаётся пуловым.
    """
    own_num = _count(own_counts or {}, num)
    own_den = _count(own_counts or {}, den)
    if own_den <= 0:
        return {**pool_rate, "own_weight": 0.0, "own_num": 0.0, "own_den": 0.0}
    weight = own_num / (own_num + MIN_RATE_EVENTS)
    own_rate = own_num / own_den
    return {
        **pool_rate,
        "rate": weight * own_rate + (1.0 - weight) * pool_rate["rate"],
        "pool_rate": pool_rate["rate"],
        "own_weight": weight,
        "own_num": own_num,
        "own_den": own_den,
    }


def _pool_counts(pools: Sequence[Tuple[str, Dict[str, Any]]],
                 name: str) -> Dict[str, Any]:
    for pool_name, counts in pools:
        if pool_name == name:
            return counts
    return {}


def _chain_variance(rates: List[Dict[str, Any]],
                    pools: Sequence[Tuple[str, Dict[str, Any]]]) -> float:
    """Относительная дисперсия составного коэффициента перехода.

    Коэффициенты ОДНОГО пула телескопируют: (paid/deals)·(deals/connected)·
    (connected/eff) — это ровно paid/eff того же пула. Складывать их
    относительные дисперсии как независимые нельзя: промежуточные счётчики
    сокращаются, и прежняя сумма 1/paid + 1/deals + … завышала
    неопределённость тем сильнее, чем ближе счётчики соседних ступеней —
    на цепочке 25/30/35/40 больше чем вдвое (аудит 2026-08-23).

    Поэтому подряд идущие пары одного пула схлопываются в одну долю
    num_самой_глубокой / den_самой_мелкой с биномиальной дисперсией
    (1−p)/num, а дисперсии РАЗНЫХ пулов складываются: пулы независимы.
    """
    variance = 0.0
    index = 0
    while index < len(rates):
        # Звено с подмешанными собственными данными не телескопирует: его
        # числитель и знаменатель больше не сокращаются с соседями. Считаем
        # его отдельно, складывая доли дисперсии по весам.
        own_weight = float(rates[index].get("own_weight") or 0.0)
        if own_weight > 0:
            rate = rates[index]
            pool_num = float(rate.get("num_events") or 0.0)
            own_num = float(rate.get("own_num") or 0.0)
            pool_var = (1.0 / pool_num) if pool_num > 0 else float("inf")
            own_var = (1.0 / own_num) if own_num > 0 else float("inf")
            variance += (own_weight ** 2) * own_var                 + ((1.0 - own_weight) ** 2) * pool_var
            index += 1
            continue
        source = rates[index]["source"]
        end = index
        while end + 1 < len(rates) and rates[end + 1]["source"] == source:
            end += 1
        counts = _pool_counts(pools, source)
        num = _count(counts, rates[index]["num"])
        den = _count(counts, rates[end]["den"])
        if num <= 0 or den <= 0:
            return float("inf")
        share = min(num / den, 1.0)
        variance += (1.0 - share) / num
        index = end + 1
    return variance


def ladder(
    counts: Dict[str, Any],
    pools: Sequence[Tuple[str, Dict[str, Any]]],
    avg_check: Optional[float] = None,
) -> Dict[str, Any]:
    """Ступень объекта + пересчёт её значения в ожидаемые оплаты и выручку."""
    step = choose_step(counts)
    events_by_step = {s: _count(counts, s) for s in STEP_ORDER}
    if step is None:
        return {"step": None, "reason": NO_STEP_REASON,
                "events_by_step": events_by_step}

    events = _count(counts, step)
    depth = STEP_ORDER.index(step)
    rates: List[Dict[str, Any]] = []
    coeff = 1.0
    for num, den in RATE_PAIRS[:depth]:
        rate = transition_rate(num, den, pools, own_counts=counts)
        if rate is None:
            return {
                "step": step, "events": events, "events_by_step": events_by_step,
                "expected_payments": None,
                "reason": f"переход {den}→{num} не оценить ни по одному пулу",
            }
        rates.append(rate)
        coeff *= rate["rate"]

    expected_payments = events * coeff
    rel_error = sqrt(1.0 / events + _chain_variance(rates, pools))

    out: Dict[str, Any] = {
        "step": step,
        "events": events,
        "events_by_step": events_by_step,
        "to_payments_coeff": round(coeff, 6),
        "expected_payments": round(expected_payments, 2),
        "rel_error": round(rel_error, 3) if rel_error != float("inf") else None,
        "rates": rates,
        "weak_rates": sum(1 for r in rates if r["weak"]),
        # Доля оценки, взятая у пула, а не у самого объекта. 1.0 значит, что
        # объект целиком описан чужими коэффициентами — решение по нему
        # держится на предположении «кампания как её направление в среднем».
        "pool_share": round(
            sum(1.0 - float(r.get("own_weight") or 0.0) for r in rates)
            / len(rates), 3) if rates else 1.0,
    }
    if avg_check is not None:
        out["expected_revenue"] = round(expected_payments * avg_check)
    return out


def ladder_report(
    objects: Dict[str, Dict[str, Any]],
    pools_by_object: Dict[str, Sequence[Tuple[str, Dict[str, Any]]]],
    avg_check_by_object: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Сводка по кабинету: у скольких объектов какая рабочая ступень.

    Это замена прежнему power.pass_decision_threshold: вместо «сколько прошло
    единственный порог» — «на какой глубине воронки может решать каждый».
    """
    by_object: Dict[str, Dict[str, Any]] = {}
    distribution: Dict[str, int] = {}
    for obj_id, counts in objects.items():
        checks = avg_check_by_object or {}
        result = ladder(counts, pools_by_object.get(obj_id, ()),
                        avg_check=checks.get(obj_id))
        by_object[obj_id] = result
        key = result["step"] or "нет_ступени"
        distribution[key] = distribution.get(key, 0) + 1
    return {
        "min_step_events": MIN_STEP_EVENTS,
        "distribution": distribution,
        "without_step": sorted(
            obj_id for obj_id, r in by_object.items() if r["step"] is None),
        "by_object": by_object,
    }
