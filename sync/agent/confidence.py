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

Порог берётся из ПАНЕЛИ НАСТРОЕК, когда она передана (thresholds), и из
константы класса, когда нет. До 25.08.2026 второй половины не было вовсе:
config.py объявлял p_sign_bid/p_sign_budget/p_sign_state, но вне самого
config.py эти ключи не встречались ни разу — решали только константы ниже.
Панель показывала ручку, которая никуда не подключена, то есть ровно то, от
чего предостерегает докстринг SPEC: «параметр, который ничего не меняет, хуже
отсутствующего». Дефолт панели равен константе кода, поэтому появление
проброса само по себе поведения не меняет — это проверяется тестом.
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

# Какой ключ панели настроек управляет каким классом действия. Отображение
# живёт здесь, а не в config.py: config импортирует портфель (за дефолтом
# explore_share), портфель импортирует эту шкалу — обратная ссылка замкнула бы
# круг импортов. Ключей три, классов пять: пороги назначены по ОБРАТИМОСТИ, и
# schedule обратим ровно так же, как bid_modifier, а autotargeting — как
# budget_shift. Разводить им отдельные ручки значило бы предлагать человеку
# различие, которого в политике нет.
CONFIG_THRESHOLD_KEYS: Dict[str, str] = {
    "bid_modifier": "p_sign_bid",
    "schedule": "p_sign_bid",
    "budget_shift": "p_sign_budget",
    "autotargeting": "p_sign_budget",
    "campaign_state": "p_sign_state",
}


def thresholds_from_config(config: Optional[Dict[str, Any]]) -> Dict[str, float]:
    """Активный конфиг прогона → пороги по классам действий.

    Конфига нет (или в нём нет нужного ключа) — класс остаётся на константе
    кода: пустой словарь здесь означает «панель ничего не переопределяет», а
    не «порога нет». Значение вне диапазона сюда не доедет — его роняет
    config._validate на разборе настройки, и глушить его второй раз тут
    значило бы делать вид, что настройка применена.
    """
    if not config:
        return {}
    out: Dict[str, float] = {}
    for action_class, key in CONFIG_THRESHOLD_KEYS.items():
        value = config.get(key)
        if value is None:
            continue
        out[action_class] = float(value)
    return out


def min_p_sign(action_class: str,
               thresholds: Optional[Dict[str, float]] = None) -> Optional[float]:
    """Действующий порог класса: из панели, иначе константа кода."""
    cls = ACTION_CLASSES.get(action_class)
    if cls is None:
        return None
    override = (thresholds or {}).get(action_class)
    return float(override) if override is not None else float(cls["min_p_sign"])


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
    thresholds: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Оценка действия против порога его класса.

    thresholds — пороги из панели настроек (thresholds_from_config). Не
    передали — действуют константы класса, то есть поведение ровно то же, что
    до появления панели.

    confident=None означает «уверенность неизвестна» (нет rel_error у строки —
    например, расчёт старого формата). Это НЕ отказ и НЕ допуск: политика
    обращения с неизвестной уверенностью решается на месте применения и обязана
    быть видна в отчёте.

    min_p_sign в ответе — ДЕЙСТВУЮЩИЙ порог, а не константа класса: по нему
    человек читает отказ в отчёте, и печатать там кодовое значение, когда
    решал настроенный, значило бы объяснять решение чужим числом.
    """
    cls = ACTION_CLASSES.get(action_class)
    if cls is None:
        return {"p_sign": None, "min_p_sign": None, "confident": False,
                "reason": UNKNOWN_CLASS_REASON.format(cls=action_class)}
    threshold = min_p_sign(action_class, thresholds)
    p = p_sign(ratio, rel_error) if rel_error is not None else None
    if p is None:
        return {"p_sign": None, "min_p_sign": threshold,
                "confident": None, "reason": "у строки нет rel_error"}
    return {"p_sign": round(p, 4), "min_p_sign": threshold,
            "confident": p >= threshold, "reason": None}
