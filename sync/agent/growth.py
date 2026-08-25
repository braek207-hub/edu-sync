# -*- coding: utf-8 -*-
"""
sync/agent/growth.py — что усилить: ответ такта на вторую половину задачи.

Агент, который умеет только сокращать, честно ведёт кабинет к «эффективно и
мало». Поэтому каждый такт обязан предъявить список усиления — даже когда
сокращать нечего.

Полный генератор гипотез — отдельная работа (Ф8 роадмапа). Здесь собираются
кандидаты из уже посчитанного: недобор трафика при живой экономике (Э7.6),
упор в кап шага (portfolio.step_cap_up), направления с растущим спросом
(demand.py) и конверсионные запросы без своей группы (objects.py).

**У кандидата обязан быть рычаг, а не только повод.** Асимметрия рычага —
свойство механики Директа, а не наша политика: вниз лимит связывает всегда,
вверх — только там, где расход уже упирается в лимит (замер: 9 кампаний из
62, docs/AGENT-AUDIT-2026-08-23.md:214). Остальным доливка не даст ничего,
единственный способ вырасти для них — поднять целевую цену конверсии. Отсюда
поле lever и ТРИ денежных итога вместо одного:

  * room_rub_budget — рубли, которые кабинет освоит сразу поднятием лимитов;
  * room_rub_tcpa   — рубли, которые освоятся только после эскалации цены
    (она сбрасывает обучение стратегии и живёт под кулдауном 14 дней), и
    считать их доступными сегодня нельзя;
  * room_rub_total  — их сумма, для человека.

Задача 11 (рост общей суммы кабинета) потребляет ИМЕННО room_rub_budget:
подставь туда total — кабинет вырастет на сумму, которую физически не
выберет, и разница осядет неосвоенным планом.

Сам этот модуль ничего не двигает: план освоения задаёт человек. Число нужно
для того, чтобы это решение принималось с цифрой на руках.
"""

from typing import Any, Dict, List, Optional

from sync.agent.demand import REGIME_RISE
from sync.agent.headroom import VERDICT_BOUGHT_OUT, VERDICT_ROOM

# Порог экономики для кандидата на усиление: предельный рубль должен как
# минимум окупаться относительно порога кабинета λ. Ниже единицы усиление
# добавляет объём ценой прибыли, а валюта решения — прибыль (portfolio.py).
MIN_ROI_VS_LAMBDA = 1.0

LEVER_BUDGET = "budget"
LEVER_TCPA = "tcpa"
# Запросу без своей группы не поможет ни лимит, ни цена: сначала нужна сама
# фраза. Отдельный рычаг, и его деньги в двух денежных итогах не участвуют.
LEVER_KEYWORD = "keyword"

SOURCE_HEADROOM = "headroom"
SOURCE_STEP_CAP = "step_cap"
SOURCE_EXPANSION = "expansion"

REASON_HEADROOM = "недобор трафика при окупающемся предельном рубле"
REASON_STEP_CAP = "солвер уткнулся в кап шага: хотел дать больше, чем можно за такт"


def growth_candidates(
    portfolio_section: Dict[str, Any],
    headroom_section: Dict[str, Dict[str, Any]],
    demand_section: Dict[str, Dict[str, Any]],
    expansion: List[Dict[str, Any]],
    quality_drift: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Кандидаты на усиление и денежный запас роста, разбитый по рычагам.

    quality_drift — карта падения качества когорты (задача 14, quality.py):
    {campaign_id: {"flagged": bool, ...}}. None означает «тормоза нет» — так
    и есть, пока задача 14 не сделана. Трактовать None как «качество упало у
    всех» нельзя: список усиления оказался бы пуст без единой причины.
    """
    rising = sorted(direction for direction, row in (demand_section or {}).items()
                    if row.get("regime") == REGIME_RISE)
    drift = quality_drift or {}

    candidates: List[Dict[str, Any]] = []
    capped = 0
    skipped_by_quality = 0
    room = {LEVER_BUDGET: 0.0, LEVER_TCPA: 0.0}

    for account in (portfolio_section.get("accounts") or {}).values():
        for campaign_id, move in (account.get("moves") or {}).items():
            campaign_id = str(campaign_id)
            headroom = headroom_section.get(campaign_id) or {}
            verdict = headroom.get("verdict")
            roi = float(move.get("marginal_roi_vs_lambda") or 0.0)
            step_capped = bool(move.get("step_capped"))
            if step_capped:
                capped += 1
            if roi < MIN_ROI_VS_LAMBDA or verdict == VERDICT_BOUGHT_OUT:
                # «Выкуплен» — это ЗАМЕР, а не молчание: показов больше нет, и
                # упор в кап шага такую кампанию кандидатом не делает.
                continue
            if verdict == VERDICT_ROOM:
                source, reason = SOURCE_HEADROOM, REASON_HEADROOM
            elif step_capped:
                source, reason = SOURCE_STEP_CAP, REASON_STEP_CAP
            else:
                continue
            if drift.get(campaign_id, {}).get("flagged"):
                # Рост не покупает мусор: новый трафик холоднее старого, и
                # ранний прокси качества успевает остановить доливку до
                # денежного чекпоинта на 35-й день (задача 14).
                skipped_by_quality += 1
                continue

            lever = LEVER_BUDGET if move.get("limit_binding") else LEVER_TCPA
            amount = max(0.0, float(move.get("target_28d") or 0.0)
                         - float(move.get("cost_28d") or 0.0))
            room[lever] += amount
            direction = move.get("direction")
            candidates.append({
                "campaign_id": campaign_id,
                "source": source,
                "reason": reason,
                "lever": lever,
                "direction": direction,
                "direction_rising": direction in rising,
                "headroom_share": headroom.get("headroom_share"),
                "roi_vs_lambda": round(roi, 2),
                "room_rub": round(amount, 2),
            })

    for item in expansion or []:
        candidates.append({
            "campaign_id": None,
            "source": SOURCE_EXPANSION,
            "reason": f"конверсионный запрос без своей группы: {item.get('query')}",
            "lever": LEVER_KEYWORD,
            "direction": None,
            "direction_rising": False,
            "headroom_share": None,
            "roi_vs_lambda": None,
            "room_rub": round(float(item.get("headroom") or 0.0), 2),
        })

    room_budget = round(room[LEVER_BUDGET], 2)
    room_tcpa = round(room[LEVER_TCPA], 2)
    return {
        "candidates": sorted(candidates, key=lambda c: -(c["room_rub"] or 0.0)),
        "capped_by_step": capped,
        "room_rub_total": round(room_budget + room_tcpa, 2),
        "room_rub_budget": room_budget,
        "room_rub_tcpa": room_tcpa,
        "directions_rising": rising,
        "skipped_by_quality": skipped_by_quality,
    }
