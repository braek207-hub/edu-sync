# -*- coding: utf-8 -*-
"""
tests/test_agent_writer_schedule.py — почасовое расписание показов.

Э0 считает почасовой профиль из Метрики, а движок записи его не применял:
на боевом прогоне 32558338766 все 23 строки schedule:hour уходили в
unsupported. Форма TimeTargeting подтверждена чтением кабинета (32560622883).

Здесь закрыты два ограничения API, каждое из которых иначе даёт отказ уровня
элемента с вечной переотправкой: кратность коэффициента десяти и запрет на
ноль (ноль означает «не показывать в этот час»).
"""

import sync.agent.writer.schedule as schedule
from sync.agent.writer.schedule import (
    HOURS,
    MAX_COEFFICIENT,
    MIN_COEFFICIENT,
    NEUTRAL,
    coefficient_from_percent,
    current_coefficients,
    hourly_coefficients,
    parse_items,
    schedule_changed,
    schedule_items,
    time_targeting_payload,
)


def _hour(hour, percent):
    return {"setting_kind": "schedule:hour", "setting_key": str(hour), "value": percent}


# --------------- коэффициент: шкала, кратность, границы

def test_percent_becomes_100_based_coefficient():
    assert coefficient_from_percent(0) == 100
    assert coefficient_from_percent(20) == 120
    assert coefficient_from_percent(-40) == 60


def test_coefficient_is_always_divisible_by_ten():
    """API принимает только кратные десяти. Расчёт даёт произвольные числа
    (+22 %, −49 %), и нецелая десятка означала бы отказ уровня элемента —
    тот же класс дефекта, что уже ловили на сегменте UNKNOWN."""
    for percent in range(-60, 61):
        assert coefficient_from_percent(percent) % 10 == 0, percent


def test_coefficient_never_reaches_zero():
    # Ноль — это «не показывать в этот час». Остановка трафика не «обратимая
    # правка ставки», такое решение принимает человек, а не автопилот.
    assert coefficient_from_percent(-100) == MIN_COEFFICIENT
    assert coefficient_from_percent(-95) >= MIN_COEFFICIENT
    assert MIN_COEFFICIENT > 0


def test_coefficient_is_capped_at_api_maximum():
    assert coefficient_from_percent(500) == MAX_COEFFICIENT


def test_rounding_is_deterministic_at_the_midpoint():
    # 25 и 35 равноудалены от десяток: выбор обязан быть один и тот же всегда,
    # иначе один расчёт даёт разные планы от прогона к прогону.
    assert coefficient_from_percent(25) == coefficient_from_percent(25)
    assert coefficient_from_percent(25) in (120, 130)
    assert coefficient_from_percent(-25) == coefficient_from_percent(-25)


# --------------- профиль по часам

def test_hours_without_data_stay_neutral():
    # Пропущенный час — «нет наблюдений», а не «показы не нужны».
    out = hourly_coefficients([_hour(3, 20)])

    assert out[3] == 120
    assert all(out[h] == NEUTRAL for h in range(HOURS) if h != 3)


def test_all_hours_are_covered():
    rows = [_hour(h, 10) for h in range(HOURS)]
    assert len(hourly_coefficients(rows)) == HOURS


def test_garbage_hour_key_is_ignored_not_crashing():
    # Ключ приходит строкой из БД; мусор не должен ронять весь прогон.
    out = hourly_coefficients([{"setting_key": "ночь", "value": 20},
                               {"setting_key": None, "value": 20},
                               {"setting_key": "99", "value": 20}])
    assert out == [NEUTRAL] * HOURS


# --------------- строки расписания

def test_items_are_25_numbers_per_day():
    items = schedule_items([_hour(0, -40)])

    assert len(items) == 7
    for day, item in zip(range(1, 8), items):
        parts = item.split(",")
        assert len(parts) == HOURS + 1
        assert parts[0] == str(day)


def test_same_profile_goes_to_every_weekday():
    """Метрика даёт распределение по часам, а не по парам (день × час).

    Ставить разное на разные дни значило бы выдумывать недельную сезонность,
    которой в данных нет.
    """
    items = schedule_items([_hour(9, 30)])
    hours_by_day = parse_items(items)

    assert len(hours_by_day) == 7
    assert len({tuple(v) for v in hours_by_day.values()}) == 1
    assert hours_by_day[1][9] == 130


def test_parse_items_survives_broken_rows():
    # Строка не той длины или с нечисловым значением не должна ронять разбор
    # чужого расписания: кампанию мог править человек или другой инструмент.
    parsed = parse_items(["1,100", "2,abc", "3," + ",".join(["100"] * 24)])
    assert list(parsed) == [3]


# --------------- сравнение с тем, что уже стоит в кабинете

def test_missing_day_in_cabinet_means_neutral_not_absent():
    # Директ отдаёт Items не для всех дней: пропуск = 100 по всем часам.
    current = current_coefficients({"Schedule": {"Items": ["1," + ",".join(["120"] * 24)]}})

    assert current[1] == [120] * 24
    assert current[5] == [NEUTRAL] * 24


def test_absent_time_targeting_is_flat_hundreds():
    assert current_coefficients(None)[3] == [NEUTRAL] * 24
    assert current_coefficients({})[3] == [NEUTRAL] * 24


def test_no_request_when_schedule_already_matches():
    """Повторная отправка того же расписания не безобидна: это запрос, риск и
    строка в журнале, а на стороне Директа ещё и сброс обучения стратегии."""
    items = schedule_items([_hour(2, -30)])
    assert schedule_changed({"Schedule": {"Items": items}}, items) is False


def test_change_is_detected():
    assert schedule_changed(None, schedule_items([_hour(2, -30)])) is True


# --------------- тело запроса

def test_neighbour_fields_are_carried_over_untouched():
    """HolidaysSchedule и ConsiderWorkingWeekends настроены человеком.

    Отправить блок без них значило бы молча сбросить чужие настройки —
    праздничный режим кампании в том числе.
    """
    current = {
        "Schedule": {"Items": ["1," + ",".join(["100"] * 24)]},
        "HolidaysSchedule": {"SuspendOnHolidays": "YES"},
        "ConsiderWorkingWeekends": "YES",
    }
    payload = time_targeting_payload(current, schedule_items([_hour(1, 20)]))

    assert payload["HolidaysSchedule"] == {"SuspendOnHolidays": "YES"}
    assert payload["ConsiderWorkingWeekends"] == "YES"
    assert payload["Schedule"]["Items"][0].startswith("1,")


def test_absent_neighbour_fields_are_not_invented():
    # Пустой HolidaysSchedule в кабинете (null) — это не повод отправить
    # что-то своё: у поля есть смысл «праздники не настроены».
    payload = time_targeting_payload({"HolidaysSchedule": None},
                                     schedule_items([_hour(1, 20)]))
    assert set(payload) == {"Schedule"}


def test_describe_counts_raised_and_lowered_hours():
    items = schedule_items([_hour(1, 30), _hour(2, -30), _hour(3, 30)])
    up, down, neutral = schedule.describe(items)

    assert (up, down, neutral) == (2, 1, 21)
