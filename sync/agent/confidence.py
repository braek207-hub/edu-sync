# -*- coding: utf-8 -*-
"""
sync/agent/confidence.py — Э2.3: шкала уверенности против класса действия.

Каждая оценка агента — число с ошибкой (rel_error едет в строке из расчёта).
Решение «применять или нет» — это сравнение уверенности В ЗНАКЕ эффекта с
порогом класса действия: дешёвое обратимое действие можно делать при умеренной
уверенности (ошибку поймает сторож и откатит), необратимое — только при почти
полной.

Вероятность знака: оценка отношения к базе (raw_value) с относительной ошибкой
rel_error, лог-нормальное приближение — z = |ln(ratio)| / rel_error, p = Φ(z).
Отношение 1.0 даёт p = 0.5 («не знаем, куда»), и такое действие не проходит
никакой порог, что верно: корректировка 0 % и так не действие.

Пороги классов — ПОЛИТИКА, а не вывод из данных (источника нет: цена ошибки
класса задаётся владельцем системы). Логика выбора записана рядом с числом;
менять пороги можно только вместе с комментарием почему.
"""

from math import erf, log, sqrt
from typing import Any, Dict, Optional

# Классы действий: порог вероятности знака и обратимость. Обратимость — то,
# что делает низкий порог допустимым: у bid_modifier есть previous_state в
# журнале, сторож и автооткат, поэтому ошибка знака стоит недели одного
# сегмента одной кампании. У campaign_state (вкл/выкл) откат НЕ возвращает
# прошлое: стратегия кампании теряет обучение — порог почти детерминированный.
ACTION_CLASSES: Dict[str, Dict[str, Any]] = {
    # 4 из 5 применённых корректировок верны по знаку; ошибочную ловит сторож.
    "bid_modifier":   {"min_p_sign": 0.80, "reversibility": "высокая"},
    "schedule":       {"min_p_sign": 0.80, "reversibility": "высокая"},
    # Деньги между кампаниями: откат возможен, но неделя бюджета уже потрачена.
    "budget_shift":   {"min_p_sign": 0.90, "reversibility": "средняя"},
    # Категории автотаргетинга меняют закупку запросов; откат восстанавливает
    # настройку, но не потерянные/лишние показы недели.
    "autotargeting":  {"min_p_sign": 0.90, "reversibility": "средняя"},
    # Остановка теряет обучение стратегии — эффект не отматывается.
    "campaign_state": {"min_p_sign": 0.97, "reversibility": "низкая"},
}

UNKNOWN_CLASS_REASON = "неизвестный класс действия: {cls} — порог не определён"


def p_sign(ratio: float, rel_error: float) -> Optional[float]:
    """Вероятность, что истинное отношение по ту же сторону от 1, что и оценка.

    None — уверенность не посчитать (нет ошибки или вырожденный вход): это
    «неизвестно», а не «уверены»; трактовку выбирает вызывающий явно.
    """
    if rel_error is None or rel_error <= 0 or ratio is None or ratio <= 0:
        return None
    z = abs(log(ratio)) / rel_error
    return 0.5 * (1.0 + erf(z / sqrt(2.0)))


def assess(
    ratio: float, rel_error: Optional[float], action_class: str,
) -> Dict[str, Any]:
    """Оценка действия против порога его класса.

    confident=None означает «уверенность неизвестна» (нет rel_error у строки —
    например, расчёт старого формата). Это НЕ отказ и НЕ допуск: политика
    обращения с неизвестной уверенностью решается на месте применения и обязана
    быть видна в отчёте.
    """
    cls = ACTION_CLASSES.get(action_class)
    if cls is None:
        return {"p_sign": None, "min_p_sign": None, "confident": False,
                "reason": UNKNOWN_CLASS_REASON.format(cls=action_class)}
    p = p_sign(ratio, rel_error) if rel_error is not None else None
    if p is None:
        return {"p_sign": None, "min_p_sign": cls["min_p_sign"],
                "confident": None, "reason": "у строки нет rel_error"}
    return {"p_sign": round(p, 4), "min_p_sign": cls["min_p_sign"],
            "confident": p >= cls["min_p_sign"], "reason": None}
