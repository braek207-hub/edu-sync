# -*- coding: utf-8 -*-
"""
sync/agent/gate.py — гейт качества данных для ПИШУЩИХ прогонов (Э1a).

Расчёт (agent_e0) гейтует свои входы сам и лишь считает числа. Прогоны с
правом записи — прямое применение (agent_e1) и сторож отката
(agent_e1_watchdog) — МЕНЯЮТ КАБИНЕТ, опираясь на витрину фактов и источник
Директа; судить о кабинете по мёртвым или битым данным им нельзя. Общий гейт
живёт здесь, а не в сторожe, чтобы обоим прогонам доставался один и тот же
критерий, а не два дрейфующих порознь.

Три слоя, от дешёвого к дорогому:
  * mart_gate — свежесть и ШИРИНА витрины фактов (по дням, медианный эталон);
  * with_source_checks — сверка сумм источник↔витрина и аномалия дневного
    объёма источника (аномалия — только для планировщика, см. докстринг);
  * data_gate — сборка полного гейта для agent_e1 одним вызовом.

Красный гейт запрещает ЗАПИСЬ, но не наблюдение: смотреть на плохие данные
можно, действовать по ним — нет.
"""

import json
import math
from datetime import date, timedelta
from statistics import median
from typing import Any, Dict, List, Optional

from sync.agent import db as agent_db
from sync.agent.guard import (
    check_freshness,
    check_sum_reconciliation,
    check_volume_anomaly,
    verdict as guard_verdict,
)

# Доля кампаний витрины, которую обязан покрывать день, считающийся «свежим».
# Гейт свежести смотрел на максимум даты по ВСЕМ кампаниям сразу: одна живая
# кампания красила его зелёным при мёртвых остальных. Обратная крайность —
# требовать свежести от КАЖДОЙ кампании — воспроизводит ошибку покрытия по
# дням кампании: у кампании, которую правка агента придушила до нуля, свежих
# строк нет, и красный гейт запретил бы откат именно там, где он нужен.
# Поэтому порог по ШИРИНЕ дня — доля от ТИПИЧНОГО дня витрины, ниже которой
# день считается неполным. Не доля от объединения кампаний за окно: за две
# недели в витрине копятся все кампании, которые хоть раз откручивались, а в
# отдельный день активна лишь часть — порог от объединения недостижим ни в
# один день, и гейт вставал бы в вечный отказ.
GATE_MIN_BREADTH = 0.5

# Витрина фактов обязана быть свежее этого возраста, иначе пишущий прогон
# СМОТРИТ, но НЕ ПИШЕТ. Порог в сутках, а не в часах: витрина дневная, и её
# последний день при живом синке — вчерашний.
FACTS_MAX_AGE_DAYS = 2

# Окно, по которому считаются ширина витрины и сверка с источником. Столько
# же, сколько горизонт наблюдения сторожа: гейт судит о данных, на которых
# выносятся вердикты, а они живут ровно в этом окне.
GATE_WINDOW_DAYS = 14


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _daily_cost(direct_rows: List[Dict[str, Any]]) -> List[tuple]:
    """Расход источника по дням, по возрастанию даты: [(дата, сумма), ...].

    Тот же приём, что у гейта расчёта (agent_e0._daily_cost): дни считаются
    по строкам источника, а не по витрине, — гейт обязан судить о том, что
    пришло, а не о том, что мы записали.
    """
    by_day: Dict[str, float] = {}
    for row in direct_rows:
        by_day[str(row["date"])] = (by_day.get(str(row["date"]), 0.0)
                                    + float(row.get("cost") or 0.0))
    return sorted(by_day.items())


def mart_gate(mart: Dict[str, Any], today: date,
              max_age_days: int = FACTS_MAX_AGE_DAYS) -> Dict[str, Any]:
    """Гейт свежести и ширины витрины фактов.

    Витрину наполняет agent_e0; перестанет запускаться — пишущий прогон будет
    исправно судить по мёртвым данным: нули вместо лидов, завышенный CPA,
    ложные вердикты. Поэтому свежесть проверяется на каждом прогоне.

    Свежесть считается не по максимуму даты среди всех кампаний: одна живая
    кампания красила бы гейт зелёным при мёртвых остальных. Свежим считается
    последний день, за который витрина наполнена по БОЛЬШИНСТВУ известных ей
    кампаний (GATE_MIN_BREADTH) — так виден и обрыв расчёта целиком, и его
    частичный отказ, но остановка одной кампании гейт не роняет.

    Ширина считается по ВСЕЙ витрине (agent_db.load_mart_day_breadth), а не по
    кампаниям открытых действий: иначе «большинство известных кампаний»
    означало бы «большинство из тех двух-трёх, по которым сейчас есть открытые
    действия», и гейт молча становился бы термометром одной кампании.
    """
    days_raw = (mart or {}).get("days") or {}
    breadth: Dict[date, int] = {}
    for raw, count in days_raw.items():
        day = _as_date(raw)
        if day is not None:
            breadth[day] = int(count or 0)
    known = int((mart or {}).get("campaigns_total") or 0)
    newest_any: Optional[date] = max(breadth) if breadth else None

    # Эталон — МЕДИАННЫЙ день окна, а не объединение кампаний за окно.
    # Медиана устойчива к краям: единичный битый день её не сдвигает, а
    # обрыв расчёта роняет её вместе со всеми днями, и гейт краснеет.
    typical = int(median(sorted(breadth.values()))) if breadth else 0
    need = max(1, math.floor(typical * GATE_MIN_BREADTH)) if typical else 0
    wide = [day for day, count in breadth.items() if count >= need] if need else []
    latest: Optional[date] = max(wide) if wide else None

    checks = check_freshness(
        {"edu_agent_facts": f"{latest.isoformat()}T00:00:00+00:00" if latest else None},
        now_iso=f"{today.isoformat()}T00:00:00+00:00",
        max_age_hours=max_age_days * 24,
    )
    status = guard_verdict(checks)
    if status == "GREEN":
        reason = ""
    elif latest is None:
        reason = ("витрина фактов пуста за окно наблюдения"
                  if newest_any is None else
                  f"ни один день витрины не наполнен по {need} кампаниям при "
                  f"типичном дне в {typical}: последняя строка есть за "
                  f"{newest_any.isoformat()}, "
                  f"но она одиночная — прогон расчёта (agent_e0) отработал не весь")
    else:
        reason = (f"последний широкий день витрины {latest.isoformat()} при пороге "
                  f"{max_age_days} дн. — прогон расчёта (agent_e0) не идёт")
    return {
        "status": status,
        "latest_fact_date": latest.isoformat() if latest else None,
        # Максимум по всем кампаниям сохранён отдельным полем: именно им гейт
        # судил раньше, и расхождение двух дат — прямая улика частичного
        # отказа расчёта, а не повод считать витрину свежей.
        "latest_any_fact_date": newest_any.isoformat() if newest_any else None,
        "campaigns_in_mart": known,
        "campaigns_typical_per_day": typical,
        "campaigns_required_per_day": need,
        "max_age_days": max_age_days,
        "reason": reason,
        "checks": checks,
    }


def with_source_checks(gate: Dict[str, Any], include_volume: bool,
                       window_days: int = GATE_WINDOW_DAYS,
                       today: Optional[date] = None) -> Dict[str, Any]:
    """Дополняет витринный гейт проверками ИСТОЧНИКА Директа.

    Сверка сумм источник↔витрина ловит витрину, собранную не из тех строк,
    которые сейчас отдаёт источник, — свежесть и ширина такого не видят: даты
    у битой витрины в порядке. Окно сверки кончается на последнем ШИРОКОМ дне
    витрины, а не на сегодня: за днями, которые витрина ещё не собрала,
    расхождение — штатный лаг сборки, а не порча.

    Аномалия дневного объёма источника (include_volume=True) гейтует только
    ПЛАНИРОВЩИКА: его оценка риска и базовый CPA считаны по этим дням, и
    планировать новые изменения на аномальном дне нельзя. Сторожа отката она
    НЕ гейтует намеренно: обвал расхода — возможное СЛЕДСТВИЕ вредного
    изменения, и замораживать откат по нему значило бы отключать защиту ровно
    в момент, когда она нужна.

    Судит эта проверка последний ПОЛНЫЙ день, а не последний день витрины.
    Расчёт (agent_e0) идёт в 09:30 МСК и кладёт в витрину сегодняшние строки;
    запись (agent_e1) идёт в 11:00 МСК и видит день, прошедший на четверть.
    Сравнение такого дня со средним по полным даёт z около шести — то есть
    красный гейт КАЖДЫЙ день, и боевая запись запрещена навсегда. Замер
    26.08.2026: mean 841 688 ₽, сегодня 89 138 ₽, z = 6.36 при пороге 4.
    Та же граница, по той же причине, стоит у окна наблюдения сторожа
    (agent_e1_watchdog.observation_window: «вчера — сегодняшний день неполон
    по расходу»).

    Отключением проверки это не является: обвал вчерашнего дня по-прежнему
    краснеет, вердикт просто приходит на день позже — ровно тогда, когда о
    дне вообще есть что сказать.

    Красный витринный гейт источником не перепроверяется: он уже красный, а
    окно сверки без последнего широкого дня не построить.
    """
    latest = gate.get("latest_fact_date")
    if not latest:
        return gate
    date_from = (date.fromisoformat(latest)
                 - timedelta(days=window_days - 1)).isoformat()
    daily = _daily_cost(agent_db.load_direct_rows(date_from, latest))
    checks: List[Dict[str, Any]] = []
    if include_volume and daily:
        complete = ([(day, cost) for day, cost in daily
                     if day < today.isoformat()] if today else daily)
        if complete:
            checks.append(check_volume_anomaly(
                [cost for _, cost in complete[:-1]], complete[-1][1]))
    if daily:
        checks.append(check_sum_reconciliation(
            sum(cost for _, cost in daily),
            agent_db.mart_cost_total(date_from, latest)))
    if not checks:
        return gate
    combined = list(gate.get("checks") or []) + checks
    status = guard_verdict(combined)
    reason = gate.get("reason") or ""
    if status == "RED" and not reason:
        reason = "; ".join(
            f"{c['check_name']}: "
            f"{json.dumps(c.get('detail'), ensure_ascii=False, default=str)}"
            for c in checks if c["status"] == "FAIL")
    return {**gate, "status": status, "reason": reason, "checks": combined}


def data_gate(today: date, window_days: int = GATE_WINDOW_DAYS,
              max_age_days: int = FACTS_MAX_AGE_DAYS,
              include_volume: bool = True) -> Dict[str, Any]:
    """Полный гейт данных для прямого применения (agent_e1), одним вызовом."""
    mart = agent_db.load_mart_day_breadth(
        (today - timedelta(days=window_days)).isoformat(), today.isoformat())
    return with_source_checks(mart_gate(mart, today, max_age_days),
                              include_volume=include_volume,
                              window_days=window_days, today=today)
