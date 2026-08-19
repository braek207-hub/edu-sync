# -*- coding: utf-8 -*-
"""
sync/agent/writer/plan.py — желаемое состояние кабинета.

Декларативно: план описывает, КАК ДОЛЖНО БЫТЬ, а не какие запросы слать.
Разницу считает diff, отправляет apply. Так повторный прогон безопасен —
если состояние уже совпадает, действий не будет.

Источник — edu_agent_computed_settings, посчитанные на Э0. Расписание
(schedule:*) в Э1a не применяется: у него другой механизм (TimeTargeting
в стратегии кампании), он войдёт отдельной задачей позже.
"""

from typing import Any, Dict, List

# Вид вычисленной настройки → тип корректировки в API Директа.
SETTING_KIND_MAP: Dict[str, str] = {
    "bid_modifier:device": "MOBILE_ADJUSTMENT",
    "bid_modifier:gender": "DEMOGRAPHICS_ADJUSTMENT",
    "bid_modifier:age": "DEMOGRAPHICS_ADJUSTMENT",
    "bid_modifier:region": "REGIONAL_ADJUSTMENT",
}

MIN_SUPPORT = 100        # ниже — не трогаем, даже если сжатие дало заметное значение
MIN_ABS_PERCENT = 5      # корректировка меньше ±5% не стоит запроса и риска


def desired_bid_modifiers(
    computed: List[Dict[str, Any]],
    min_support: int = MIN_SUPPORT,
    min_abs_percent: int = MIN_ABS_PERCENT,
) -> List[Dict[str, Any]]:
    """Желаемые корректировки из вычисленных настроек."""
    out: List[Dict[str, Any]] = []
    for row in computed:
        kind = str(row.get("setting_kind") or "")
        if kind not in SETTING_KIND_MAP:
            continue
        if int(row.get("support_n") or 0) < min_support:
            continue
        percent = int(round(float(row.get("value") or 0.0)))
        if abs(percent) < min_abs_percent:
            continue
        out.append({
            "kind": kind,
            "direct_type": SETTING_KIND_MAP[kind],
            "key": str(row.get("setting_key")),
            "percent": percent,
        })
    return sorted(out, key=lambda r: (r["kind"], r["key"]))
