# -*- coding: utf-8 -*-
"""
sync/agent/mining.py — квазиэксперименты из истории кабинета.

За окно истории уже произошли сотни изменений: менялись бюджеты, ставки, стратегии.
Каждое — естественный эксперимент, который уже оплачен и уже дал результат.
Находим момент изменения по скачку управляемого параметра и меряем эффект через DiD
против остальных кампаний за тот же период — сезон вычитается сам.

Смещение осознанное: трогали обычно то, что уже плохо работало, а плохое само
склонно вернуться к среднему. Поэтому класс надёжности B (не A) и умышленно широкий
доверительный интервал: такие оценки задают приоритет гипотез, но не дают права
двигать большие деньги.
"""

import hashlib
from datetime import date
from statistics import mean
from typing import Any, Dict, List, Optional

MIN_SIDE_DAYS = 7          # минимум дней с каждой стороны точки изменения
QUASI_CI_WIDTH = 0.5       # ширина интервала для квазиоценки (доля от эффекта + запас)


def detect_change_points(series: List[Dict[str, Any]], min_jump: float = 0.3) -> List[Dict[str, Any]]:
    """Точки скачка среднего уровня ряда: |после − до| / до ≥ min_jump."""
    ordered = sorted(series, key=lambda r: r["date"])
    if len(ordered) < MIN_SIDE_DAYS * 2:
        return []
    points: List[Dict[str, Any]] = []
    for i in range(MIN_SIDE_DAYS, len(ordered) - MIN_SIDE_DAYS + 1):
        before = mean(float(r["value"]) for r in ordered[i - MIN_SIDE_DAYS:i])
        after = mean(float(r["value"]) for r in ordered[i:i + MIN_SIDE_DAYS])
        if before <= 0:
            continue
        jump = abs(after - before) / before
        if jump >= min_jump:
            points.append({
                "date": ordered[i]["date"],
                "before": round(before, 2),
                "after": round(after, 2),
                "jump": round(jump, 4),
            })
    # Схлопываем соседние срабатывания одного и того же скачка: скользящее окно
    # реагирует на каждый день вокруг ступени. Группируем по близости дат и берём
    # вершину — момент, где отношение «после/до» максимально, и есть дата изменения.
    deduped: List[Dict[str, Any]] = []
    group: List[Dict[str, Any]] = []

    def _flush() -> None:
        if group:
            deduped.append(max(group, key=lambda p: p["jump"]))
            group.clear()

    for p in points:
        if group:
            prev_day = date.fromisoformat(group[-1]["date"])
            curr_day = date.fromisoformat(p["date"])
            if (curr_day - prev_day).days > MIN_SIDE_DAYS:
                _flush()
        group.append(p)
    _flush()
    return deduped


def did_effect(
    treated_before: float, treated_after: float, control_before: float, control_after: float
) -> Dict[str, Optional[float]]:
    """Difference-in-differences: изменение у обработанной минус изменение у контроля."""
    if treated_before <= 0 or control_before <= 0:
        return {"effect": None, "effect_lo": None, "effect_hi": None}
    treated_delta = (treated_after - treated_before) / treated_before
    control_delta = (control_after - control_before) / control_before
    effect = treated_delta - control_delta
    margin = abs(effect) * QUASI_CI_WIDTH + 0.05
    return {
        "effect": round(effect, 4),
        "effect_lo": round(effect - margin, 4),
        "effect_hi": round(effect + margin, 4),
    }


def _quality(rows: List[Dict[str, Any]]) -> float:
    cost = sum(float(r.get("cost") or 0.0) for r in rows)
    expected = sum(float(r.get("sum_p_pay") or 0.0) for r in rows)
    return cost / expected if expected > 0 else 0.0


def _experiment_id(campaign_id: str, change_date: str) -> str:
    raw = f"quasi:{campaign_id}:{change_date}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def mine_quasi_experiments(facts: List[Dict[str, Any]], window: int = 14) -> List[Dict[str, Any]]:
    """Находит изменения бюджета в истории и меряет их эффект через DiD."""
    by_campaign: Dict[str, List[Dict[str, Any]]] = {}
    for f in facts:
        by_campaign.setdefault(str(f["campaign_id"]), []).append(f)

    out: List[Dict[str, Any]] = []
    for campaign_id, rows in sorted(by_campaign.items()):
        series = [{"date": r["fact_date"], "value": float(r.get("cost") or 0.0)} for r in rows]
        for point in detect_change_points(series):
            change_date = point["date"]
            treated_before = [r for r in rows if r["fact_date"] < change_date][-window:]
            treated_after = [r for r in rows if r["fact_date"] >= change_date][:window]
            control_rows = [f for f in facts if str(f["campaign_id"]) != campaign_id]
            control_before = [r for r in control_rows if r["fact_date"] < change_date][-window:]
            control_after = [r for r in control_rows if r["fact_date"] >= change_date][:window]
            if not (treated_before and treated_after and control_before and control_after):
                continue

            measured = did_effect(
                _quality(treated_before), _quality(treated_after),
                _quality(control_before), _quality(control_after),
            )
            if measured["effect"] is None:
                continue

            out.append({
                "experiment_id": _experiment_id(campaign_id, change_date),
                "hypothesis_type": "budget_change",
                "object_level": "campaign",
                "object_id": campaign_id,
                "params": {"jump": point["jump"], "before": point["before"], "after": point["after"]},
                "mechanism": "did",
                "started_on": change_date,
                "measured_on": change_date,
                "metric": "cost_per_expected_payment",
                "verdict": "improved" if measured["effect"] < 0 else "worsened",
                "reliability_class": "B",
                "source": "quasi",
                **measured,
            })
    return out
