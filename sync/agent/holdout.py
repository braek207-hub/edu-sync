# -*- coding: utf-8 -*-
"""
sync/agent/holdout.py — формирование заповедника.

Заповедник нужен дважды: как эталон эффекта (DiD вычитает сезон) и как непредвзятая
выборка для валидации ML — модель обучается на лидах, приведённых агентом, который
оптимизирован под эту же модель, и без чистого зеркала петля самоподтверждения
не ловится.

Отбор стратифицирован по направлению и по величине расхода (тертили), чтобы в
заповеднике оказались и крупные, и мелкие кампании: если в нём только дно, база
сравнения кривая и заслуга агента завышена. Детерминирован по хешу id — повторный
прогон обязан дать тот же состав, иначе замер поплывёт.
"""

import hashlib
from typing import Any, Dict, List

MIN_LEADS_30D = 1


def _stratum(direction: str, cost: float, thresholds: tuple) -> str:
    low, high = thresholds
    if cost <= low:
        return f"{direction}:small"
    if cost <= high:
        return f"{direction}:mid"
    return f"{direction}:large"


def _rank(campaign_id: str, seed: str) -> str:
    return hashlib.sha256(f"{seed}:{campaign_id}".encode("utf-8")).hexdigest()


def select_holdout(
    campaigns: List[Dict[str, Any]], share: float = 0.06, seed: str = "edu-2026"
) -> List[Dict[str, Any]]:
    """Стратифицированный детерминированный отбор заповедника."""
    alive = [c for c in campaigns if (c.get("leads_30d") or 0) >= MIN_LEADS_30D]
    if not alive:
        return []

    by_direction: Dict[str, List[Dict[str, Any]]] = {}
    for c in alive:
        by_direction.setdefault(c.get("direction") or "unknown", []).append(c)

    picked: List[Dict[str, Any]] = []
    for direction, items in sorted(by_direction.items()):
        costs = sorted(float(i.get("cost_30d") or 0.0) for i in items)
        third = max(len(costs) // 3, 1)
        thresholds = (costs[third - 1], costs[min(2 * third - 1, len(costs) - 1)])

        by_stratum: Dict[str, List[Dict[str, Any]]] = {}
        for item in items:
            key = _stratum(direction, float(item.get("cost_30d") or 0.0), thresholds)
            by_stratum.setdefault(key, []).append(item)

        target = max(round(len(items) * share), 1)
        per_stratum = max(target // max(len(by_stratum), 1), 1)

        for stratum, group in sorted(by_stratum.items()):
            ordered = sorted(group, key=lambda c: _rank(c["campaign_id"], seed))
            for item in ordered[:per_stratum]:
                picked.append({
                    "campaign_id": item["campaign_id"],
                    "direction": direction,
                    "stratum": stratum,
                    "reason": "стратифицированный отбор, детерминированный по хешу id",
                })

    return sorted(picked, key=lambda c: c["campaign_id"])
