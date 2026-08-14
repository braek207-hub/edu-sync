# -*- coding: utf-8 -*-
"""
sync/agent/profile.py — профиль успешной кампании и дистанция до него.

Даёт две вещи без единого эксперимента:
  1. ранжированный список «что докрутить в первую очередь» (дистанция до профиля);
  2. «ДНК» для сборки новых кампаний (Э5).

Класс надёжности выводов — C (наблюдение): это корреляция, не причинность.
Верхний квартиль мог оказаться там по другой причине, поэтому профиль задаёт
приоритет гипотез, но не даёт права двигать большие деньги.
"""

from statistics import median
from typing import Any, Dict, List, Optional, Tuple

TOP_QUARTILE = 0.25


def campaign_quality(row: Dict[str, Any]) -> Optional[float]:
    """cost / Σ p_pay — стоимость ожидаемой оплаты. Меньше = лучше."""
    expected = float(row.get("sum_p_pay") or 0.0)
    if expected <= 0:
        return None
    return float(row.get("cost") or 0.0) / expected


def build_profile(campaigns: List[Dict[str, Any]], features: List[str]) -> Dict[str, float]:
    """Медианы признаков по верхнему квартилю качества."""
    scored = [(campaign_quality(c), c) for c in campaigns]
    usable = [(q, c) for q, c in scored if q is not None]
    if not usable:
        return {}
    usable.sort(key=lambda pair: pair[0])  # меньше стоимость оплаты — лучше
    cutoff = max(int(len(usable) * TOP_QUARTILE), 1)
    top = [c for _, c in usable[:cutoff]]
    return {f: float(median([float(c.get(f) or 0.0) for c in top])) for f in features}


def distance_to_profile(
    campaign: Dict[str, Any], profile: Dict[str, float], features: List[str]
) -> Tuple[float, List[Dict[str, Any]]]:
    """Нормированная дистанция до профиля + разрывы, худший первым."""
    if not profile:
        return 0.0, []
    gaps: List[Dict[str, Any]] = []
    total = 0.0
    for f in features:
        target = float(profile.get(f) or 0.0)
        actual = float(campaign.get(f) or 0.0)
        scale = abs(target) if target else 1.0
        gap = abs(actual - target) / scale
        if gap > 0:
            gaps.append({"feature": f, "actual": actual, "target": target, "gap": round(gap, 4)})
        total += gap
    gaps.sort(key=lambda g: g["gap"], reverse=True)
    return round(total, 4), gaps
