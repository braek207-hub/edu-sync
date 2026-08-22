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


# --------------- рельсы: расписание правит кампанию целиком

from sync.agent.writer.diff import diff_schedule
from sync.agent.writer.guardrails import check_action, check_rollback
from sync.agent.writer.rollback import rollback_payload


def _action(items=None, previous=None):
    # Пустой список — валидный вход теста (проверка рельсы), поэтому подмена
    # дефолтом только для None: `items or ...` съедал бы именно этот случай.
    if items is None:
        items = schedule_items([_hour(3, -40)])
    return {
        "action_kind": "schedule.set",
        "object_id": "111",
        "payload": {"CampaignId": 111,
                    "TimeTargeting": {"Schedule": {"Items": items}}},
        "previous_state": {"TimeTargeting": previous if previous is not None else {}},
    }


def test_correct_schedule_passes_the_rail():
    ok, reason = check_action(_action())
    assert ok, reason


def test_zero_hour_is_refused_by_the_rail():
    """Ноль в расписании — не «ставка ниже», а «показов в этот час нет».

    Ошибка построителя обернулась бы выключенным трафиком, поэтому рельса
    считает независимо от него: проверка, доверяющая тому, кого проверяет,
    не проверка.
    """
    broken = ["1," + ",".join(["0"] + ["100"] * 23)]
    ok, reason = check_action(_action(items=broken))

    assert not ok
    assert "выключает показы" in reason


def test_non_multiple_of_ten_is_refused():
    broken = ["1," + ",".join(["122"] + ["100"] * 23)]
    ok, reason = check_action(_action(items=broken))

    assert not ok
    assert "кратен" in reason


def test_out_of_range_coefficient_is_refused():
    broken = ["1," + ",".join(["300"] + ["100"] * 23)]
    assert check_action(_action(items=broken))[0] is False


def test_short_row_is_refused():
    assert check_action(_action(items=["1,100,100"]))[0] is False


def test_empty_schedule_is_refused():
    assert check_action(_action(items=[]))[0] is False


def test_bad_weekday_is_refused():
    broken = ["9," + ",".join(["100"] * 24)]
    assert check_action(_action(items=broken))[0] is False


# --------------- действие создаётся только при реальном отличии

def test_no_action_when_cabinet_already_matches():
    items = schedule_items([_hour(3, -40)])
    assert diff_schedule(items, {"Schedule": {"Items": items}}, "111") == []


def test_action_carries_the_whole_previous_block():
    """previous_state несёт ВЕСЬ прежний TimeTargeting, а не только часы.

    Вместе с расписанием в блоке живут праздничный режим и учёт рабочих
    выходных, настроенные человеком. Сохрани мы одни часы — откат собрал бы
    блок заново и стёр бы их, то есть сам стал бы правкой.
    """
    current = {"Schedule": {"Items": ["1," + ",".join(["100"] * 24)]},
               "HolidaysSchedule": {"SuspendOnHolidays": "YES"},
               "ConsiderWorkingWeekends": "YES"}
    actions = diff_schedule(schedule_items([_hour(3, -40)]), current, "111")

    assert len(actions) == 1
    assert actions[0]["previous_state"]["TimeTargeting"] == current
    assert actions[0]["direct_type"] == "TIME_TARGETING"


def test_idempotency_key_follows_the_profile():
    # Ключ от содержимого: пересчитал Э0 хоть один час — это другое действие,
    # и закрытый ключ прошлого прогона его не отсечёт.
    first = diff_schedule(schedule_items([_hour(3, -40)]), {}, "111")[0]
    second = diff_schedule(schedule_items([_hour(3, -30)]), {}, "111")[0]

    assert first["idempotency_key"] != second["idempotency_key"]


# --------------- откат возвращает прежний блок целиком

def test_rollback_restores_the_previous_block():
    previous = {"Schedule": {"Items": ["1," + ",".join(["100"] * 24)]},
                "HolidaysSchedule": {"SuspendOnHolidays": "YES"}}
    service, method, params = rollback_payload(_action(previous=previous))

    assert (service, method) == ("campaigns", "update")
    assert params["Campaigns"][0]["TimeTargeting"] == previous
    assert params["Campaigns"][0]["Id"] == 111


def test_rollback_is_refused_without_known_previous_state():
    # Вслепую не пишем — то же правило, что у корректировки без Id.
    action = _action()
    action["previous_state"] = {}
    assert rollback_payload(action) is None


def test_rollback_of_schedule_passes_its_rail():
    ok, reason = check_rollback({"action_kind": "schedule.set"})
    assert ok, reason
