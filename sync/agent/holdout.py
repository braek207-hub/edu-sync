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

Три правила, каждое куплено замером docs/AGENT-TICK-POWER.md (29.08.2026):

  * КВОТА НАПРАВЛЕНИЯ — ПО ДЕНЬГАМ. Прежний обход направлений по кругу шёл в
    алфавитном порядке, а глобальная цель при 91 живой кампании и доле 6 % —
    пять мест. Отсортированный список направлений — dist, it, med, ntb, other,
    school, spo, transfer, vpo, и до седьмого элемента очередь не доходила
    НИКОГДА: spo (27 кампаний) и vpo (22) — 64 % расхода кабинета — оставались
    без контроля вообще, а «сезон», который вычитается из их движения, мерился
    на кампаниях совсем других направлений. Прогоны отбора на срезах 14.08 и
    28.08 дали ровно ['dist','it','med','ntb','other'] — это не жребий, а
    построение. Места делятся по доле расхода методом наибольших остатков.
  * МЁРТВЫЕ ВЫБЫВАЮТ (dead_holdout_ids). Когорта, отобранная 15.05, за ОДИН
    месяц потеряла 4 кампании из 9, а её эффективный размер упал с 5,19 до
    2,93; к +60 дням — до 1,47. Разрыв цены лида между заповедником и остальным
    кабинетом при этом уехал с −19 % на +25 %, то есть на 44 процентных пункта —
    ровно та величина, постоянство которой предполагает DiD. Мёртвая кампания,
    оставленная контролем навсегда, — это не «стабильный состав», а окаменевший
    шум.
  * ГОДНОСТЬ КОНТРОЛЯ — ПО ЭФФЕКТИВНОМУ РАЗМЕРУ (kish_n_eff), а не по сумме
    лидов: 69 лидов в одной кампании и 69 в пяти — разные контроли.
"""

import hashlib
from typing import Any, Dict, Iterable, List

MIN_LEADS_30D = 1

# Минимум эффективных лидов заповедника в окне, чтобы контроль вообще считался
# контролем. Порог живёт ЗДЕСЬ, а не у того, кто им пользуется: контролем
# заповедник работает в двух местах сразу — у сторожа при разности разностей
# одного действия (agent_e1_watchdog.holdout_control) и у замера такта целиком
# (tact_effect.measure), — и двум копиям числа достаточно одной правки, чтобы
# разойтись и назвать контролем разное. На десятке лидов цена конверсии — шум,
# и вычтенный по такому контролю «сезон» добавил бы к оценке случайное число
# вместо поправки.
MIN_CONTROL_LEADS = 20

# Минимальный ЭФФЕКТИВНЫЙ размер контроля (Kish n_eff по эффективным лидам
# кампаний заповедника в окне). Порога на сумму лидов мало: метрика контроля —
# отношение суммы расхода к сумме лидов, то есть взвешенное среднее, и его
# разброс задаёт не число кампаний, а то, насколько равномерно между ними
# распределён вес. Замер docs/AGENT-TICK-POWER.md: фактический заповедник из
# 9 живых кампаний имеет n_eff = 4,74 и разброс движения 18,8 % — уже шумнее
# тех 11,2 %, которые он поправляет; когорта, дожившая до +60 дней, даёт
# n_eff 1,47 при 453 лидах, то есть проходит порог в 20 лидов с запасом в
# двадцать раз, будучи фактически ОДНОЙ кампанией. Порог 3,0 отсекает именно
# это: ниже него «контроль» — это одна-две кампании, и вычитается из такта не
# сезон, а их собственная история.
MIN_CONTROL_NEFF = 3.0


def kish_n_eff(weights: Iterable[float]) -> float:
    """Эффективный размер выборки по Кишу: (Σw)² / Σw².

    Сколько РАВНЫХ по весу кампаний дали бы такой же разброс взвешенного
    среднего, как эта неравная группа. Для десяти одинаковых кампаний это
    десять, для девяти, где одна даёт 80 % лидов, — меньше двух. Именно это
    число, а не длина списка, отвечает на вопрос «на скольких кампаниях
    стоит контроль».
    """
    values = [float(w) for w in weights if float(w or 0.0) > 0]
    if not values:
        return 0.0
    total = sum(values)
    return (total * total) / sum(v * v for v in values)


def dead_holdout_ids(holdout_ids: Iterable[str],
                     campaigns: List[Dict[str, Any]]) -> List[str]:
    """Кампании заповедника, переставшие быть кампаниями: нет лидов за окно.

    Критерий тот же, что у отбора (MIN_LEADS_30D): кто не годился бы в
    заповедник сегодня, тот не годится и оставаться в нём. Кампания, которой
    вовсе нет в агрегатах окна, мертва тем более — не молчание витрины, а
    отсутствие открутки.

    Возвращает список для пометки excluded_at. Держать мёртвых в контроле
    дешевле всего кажется («состав не меняем»), но именно так контроль из
    девяти кампаний за два месяца превращается в полторы
    (docs/AGENT-TICK-POWER.md).
    """
    alive = {str(c.get("campaign_id")) for c in campaigns or ()
             if (c.get("leads_30d") or 0) >= MIN_LEADS_30D}
    return sorted({str(i) for i in holdout_ids or ()} - alive)


def _stratum(direction: str, cost: float, thresholds: tuple) -> str:
    low, high = thresholds
    if cost <= low:
        return f"{direction}:small"
    if cost <= high:
        return f"{direction}:mid"
    return f"{direction}:large"


def _rank(campaign_id: str, seed: str) -> str:
    return hashlib.sha256(f"{seed}:{campaign_id}".encode("utf-8")).hexdigest()


def _direction_quotas(weights: Dict[str, float], capacity: Dict[str, int],
                      target: int) -> Dict[str, int]:
    """Сколько мест заповедника достаётся каждому направлению.

    Метод наибольших остатков (Гамильтона): целые части точной доли раздаются
    сразу, оставшиеся места — направлениям с наибольшим дробным хвостом. Так
    контроль повторяет кабинет ПО ДЕНЬГАМ: при пяти местах и долях расхода
    vpo 36 %, spo 26 %, other 16 % три четверти кабинета получают контроль, а
    не остаются без него из-за буквы в имени направления.

    Вес — расход, а не число кампаний: замер такта сравнивает цену лида,
    взвешенную расходом, и направление на 9,5 млн ₽ двигает эту цену сильнее,
    чем пять направлений по 200 тыс. Расхода нет ни у кого (тестовый кабинет,
    пустая витрина) — вес по числу живых кампаний, иначе отбор молча вернул бы
    пустоту.

    Порядок раздачи детерминирован до последнего тай-брейка (остаток, вес,
    имя): повторный прогон обязан дать тот же состав, иначе база сравнения
    поплывёт между прогонами.
    """
    positive = {d: max(float(w or 0.0), 0.0) for d, w in weights.items()}
    total = sum(positive.values())
    if total <= 0:
        positive = {d: float(capacity.get(d, 0)) for d in weights}
        total = sum(positive.values())
    if total <= 0 or target <= 0:
        return {d: 0 for d in weights}

    quotas: Dict[str, int] = {}
    order: List[tuple] = []
    for direction in sorted(positive):
        exact = target * positive[direction] / total
        quotas[direction] = min(int(exact), capacity.get(direction, 0))
        order.append((exact - int(exact), positive[direction], direction))

    order.sort(key=lambda t: (-t[0], -t[1], t[2]))
    left = target - sum(quotas.values())
    while left > 0:
        handed = 0
        for _, _, direction in order:
            if left <= 0:
                break
            if quotas[direction] < capacity.get(direction, 0):
                quotas[direction] += 1
                left -= 1
                handed += 1
        if handed == 0:      # мест больше, чем живых кампаний
            break
    return quotas


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
    weights: Dict[str, float] = {}
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
        weights[direction] = sum(costs)

    # Места делятся по доле расхода направления, а не выдаются по кругу в
    # алфавитном порядке: обход по алфавиту при пяти местах и девяти
    # направлениях навсегда оставлял без контроля spo и vpo — 64 % расхода
    # кабинета (docs/AGENT-TICK-POWER.md).
    quotas = _direction_quotas(
        weights, {d: len(q) for d, q in queues.items()}, global_target)

    picked: List[Dict[str, Any]] = []
    for direction in sorted(queues):
        for item in queues[direction][:quotas.get(direction, 0)]:
            picked.append({
                "campaign_id": item["campaign_id"],
                "direction": direction,
                "stratum": item["stratum"],
                "reason": "стратифицированный отбор по доле расхода направления, "
                          "детерминированный по хешу id",
            })

    return sorted(picked, key=lambda c: c["campaign_id"])
