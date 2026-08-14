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

    # Целевой размер считается от ВСЕГО кабинета, а не по каждой страте отдельно:
    # «минимум одна на страту» при 3 стратах × N направлений раздувает заповедник
    # в разы (замер на 26% кабинета вместо 6%).
    global_target = max(round(len(alive) * share), 1)

    # Очередь кандидатов внутри направления: сначала средняя страта (не край
    # распределения), затем крупные и мелкие; внутри страты — детерминированно по хешу.
    queues: Dict[str, List[Dict[str, Any]]] = {}
    for direction, items in sorted(by_direction.items()):
        costs = sorted(float(i.get("cost_30d") or 0.0) for i in items)
        third = max(len(costs) // 3, 1)
        thresholds = (costs[third - 1], costs[min(2 * third - 1, len(costs) - 1)])

        by_stratum: Dict[str, List[Dict[str, Any]]] = {}
        for item in items:
            key = _stratum(direction, float(item.get("cost_30d") or 0.0), thresholds)
            by_stratum.setdefault(key, []).append(item)

        queue: List[Dict[str, Any]] = []
        for suffix in ("mid", "large", "small"):
            group = by_stratum.get(f"{direction}:{suffix}", [])
            for item in sorted(group, key=lambda c: _rank(c["campaign_id"], seed)):
                queue.append({**item, "stratum": f"{direction}:{suffix}"})
        queues[direction] = queue

    # Обход направлений по кругу: каждое получает представителя раньше, чем любое
    # направление получит второго — репрезентативность важнее точной пропорции.
    picked: List[Dict[str, Any]] = []
    round_index = 0
    while len(picked) < global_target and any(len(q) > round_index for q in queues.values()):
        for direction in sorted(queues):
            if len(picked) >= global_target:
                break
            queue = queues[direction]
            if len(queue) > round_index:
                item = queue[round_index]
                picked.append({
                    "campaign_id": item["campaign_id"],
                    "direction": direction,
                    "stratum": item["stratum"],
                    "reason": "стратифицированный отбор, детерминированный по хешу id",
                })
        round_index += 1

    return sorted(picked, key=lambda c: c["campaign_id"])
