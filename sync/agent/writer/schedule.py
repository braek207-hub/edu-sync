# -*- coding: utf-8 -*-
"""
sync/agent/writer/schedule.py — почасовое расписание показов кампании.

Э0 считает почасовой профиль по достижениям целей из Метрики
(agent/computed.py::compute_schedule) и кладёт его строками вида
`schedule:hour`. Движок записи их не применял: у корректировок ставок и у
расписания РАЗНЫЕ механизмы — bidmodifiers против TimeTargeting внутри самой
кампании, — и на боевом прогоне 32558338766 все 23 строки уходили в
unsupported. Здесь эта половина закрывается.

Форма подтверждена чтением боевого кабинета (probe 32560622883) и справочником:

    "TimeTargeting": {
      "Schedule": {"Items": ["1,100,100,...,100", ... до 7 строк]},
      "HolidaysSchedule": ...,
      "ConsiderWorkingWeekends": "YES"
    }

Каждая строка — 25 чисел: день недели (1 = понедельник … 7 = воскресенье) и
24 почасовых коэффициента. Шкала 100-базная, как у корректировок: 100 —
без изменений.

Два ограничения API, которые здесь и соблюдаются:

  · коэффициент обязан быть КРАТЕН 10. Расчёт даёт произвольные числа
    (+22 %, −49 %), и нецелая десятка означала бы отказ уровня элемента с
    вечной переотправкой — тот же класс дефекта, что уже ловили на UNKNOWN;
  · 0 означает «не показывать в этот час». Автопилоту такое действие
    недоступно: остановка показов не «обратимая правка ставки», а выключение
    трафика, и решать это должен человек. Поэтому пол — MIN_COEFFICIENT.

Профиль у нас один на все дни недели: Метрика даёт распределение по часам,
а не по парам (день × час). Разделять их без данных значило бы выдумывать
недельную сезонность, поэтому одинаковый набор ставится на все семь дней.
"""

from typing import Any, Dict, Iterable, List, Optional, Tuple

NEUTRAL = 100
# Потолок API — 200. Пол намеренно НЕ 0: ноль выключает показы в этот час.
MIN_COEFFICIENT = 10
MAX_COEFFICIENT = 200
STEP = 10
DAYS = (1, 2, 3, 4, 5, 6, 7)
HOURS = 24


def _round_to_step(value: int, step: int = STEP) -> int:
    """Ближайшее кратное step. При равном расстоянии — вверх, к меньшему урезанию."""
    return int(step * round(value / step + 1e-9))


def coefficient_from_percent(percent: float) -> int:
    """Дельта расчёта (−49 = «−49 %») → коэффициент API, кратный десяти."""
    raw = NEUTRAL + int(round(float(percent)))
    stepped = _round_to_step(raw)
    return max(MIN_COEFFICIENT, min(MAX_COEFFICIENT, stepped))


def hourly_coefficients(rows: Iterable[Dict[str, Any]]) -> List[int]:
    """24 коэффициента по часам. Час без данных остаётся нейтральным.

    Пропущенный час — это «нет наблюдений», а не «показы не нужны»: ставить
    туда что-либо кроме 100 значило бы выдумывать число.
    """
    out = [NEUTRAL] * HOURS
    for row in rows or []:
        try:
            hour = int(str(row.get("setting_key")))
        except (TypeError, ValueError):
            continue
        if 0 <= hour < HOURS:
            out[hour] = coefficient_from_percent(row.get("value") or 0)
    return out


def schedule_items(rows: Iterable[Dict[str, Any]]) -> List[str]:
    """Почасовой профиль → Items для TimeTargeting: по строке на день недели."""
    coefficients = hourly_coefficients(rows)
    body = ",".join(str(c) for c in coefficients)
    return [f"{day},{body}" for day in DAYS]


def parse_items(items: Iterable[str]) -> Dict[int, List[int]]:
    """Items из кабинета → {день недели: [24 коэффициента]}.

    Нужен для сравнения желаемого с фактическим: без разбора текущего
    расписания каждый прогон переписывал бы кампанию одним и тем же значением.
    """
    out: Dict[int, List[int]] = {}
    for item in items or []:
        parts = [p.strip() for p in str(item).split(",")]
        if len(parts) != HOURS + 1:
            continue
        try:
            numbers = [int(p) for p in parts]
        except ValueError:
            continue
        day, hours = numbers[0], numbers[1:]
        if day in DAYS:
            out[day] = hours
    return out


def current_coefficients(time_targeting: Optional[Dict[str, Any]]) -> Dict[int, List[int]]:
    """Текущее расписание кампании. Отсутствие блока — ровные сотни.

    Директ отдаёт Items не для всех дней: пропущенный день означает 100 по
    всем часам (подтверждено справочником). Поэтому пропуск разворачивается
    в нейтральный ряд, а не считается «данных нет».
    """
    schedule = ((time_targeting or {}).get("Schedule") or {}).get("Items") or []
    parsed = parse_items(schedule)
    return {day: parsed.get(day, [NEUTRAL] * HOURS) for day in DAYS}


def schedule_changed(
    time_targeting: Optional[Dict[str, Any]], desired_items: List[str]
) -> bool:
    """Нужно ли вообще слать запрос: расписание уже такое — не нужно.

    Повторная отправка того же расписания не безобидна: каждая правка
    кампании — это запрос, риск-бюджет и строка в журнале, а на стороне
    Директа ещё и сброс статистики обучения стратегии.
    """
    return current_coefficients(time_targeting) != current_coefficients(
        {"Schedule": {"Items": desired_items}})


def time_targeting_payload(
    time_targeting: Optional[Dict[str, Any]], desired_items: List[str]
) -> Dict[str, Any]:
    """Тело TimeTargeting для campaigns.update.

    Соседние поля блока (HolidaysSchedule, ConsiderWorkingWeekends) переносятся
    КАК ЕСТЬ. Они настроены человеком и к почасовому профилю отношения не
    имеют; отправить блок без них значило бы молча сбросить чужие настройки —
    праздничный режим кампании в том числе.
    """
    payload: Dict[str, Any] = {"Schedule": {"Items": list(desired_items)}}
    for field in ("HolidaysSchedule", "ConsiderWorkingWeekends"):
        value = (time_targeting or {}).get(field)
        if value is not None:
            payload[field] = value
    return payload


def describe(desired_items: List[str]) -> Tuple[int, int, int]:
    """(сколько часов поднято, сколько опущено, сколько нейтрально) — для отчёта."""
    hours = parse_items(desired_items).get(DAYS[0], [NEUTRAL] * HOURS)
    up = sum(1 for h in hours if h > NEUTRAL)
    down = sum(1 for h in hours if h < NEUTRAL)
    return up, down, HOURS - up - down
