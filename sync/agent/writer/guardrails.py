# -*- coding: utf-8 -*-
"""
sync/agent/writer/guardrails.py — рельсы движка записи (слой 2 защиты).

Ограничения, которые агент не пересекает никогда — независимо от того, что
насчитала статистика и что предложил LLM:

  - удаление объектов запрещено (пауза вместо удаления);
  - корректировка ставки ограничена ±50%: расчёт уже сжат к нулю, всё что
    выходит за коридор — признак ошибки, а не находки;
  - заповедник неприкосновенен: его кампании не получают ни одного действия,
    иначе база сравнения для всех замеров теряется;
  - число действий за прогон ограничено: массовое изменение невозможно
    проверить сторожем и невозможно осмысленно откатить;
  - откат обязан возвращать ИМЕННО в прошлое состояние журнала, а не в любое
    значение, которое примет API: рельса возврата сверяет намерение, а не
    только форму запроса (check_rollback).
"""

from typing import Any, Dict, List, Optional, Set, Tuple

from sync.agent.writer.units import API_MAX, API_MIN, API_NEUTRAL

# Рельса работает по ДЕЛЬТЕ, а не по 100-базному коэффициенту Директа: в
# payload действия (diff.py) лежит внутренняя единица движка, перевод в шкалу
# API делается позже и только один раз — в apply.to_api_call через
# units.delta_to_api. Смысл рельсы: не выпускать корректировку за ±50 % от
# исходной ставки, то есть |дельта| <= 50 (в шкале API это коридор 50..150).
# Та же цифра, приложенная к 100-базе, разрешала бы 0..50 — «срезать ставку
# вдвое и сильнее», ровно наоборот к замыслу.
MODIFIER_CAP = 50          # потолок и пол корректировки, проценты (дельта)
MAX_ACTIONS_PER_RUN = 50

# Allow-лист: разрешено ровно это, всё остальное отклоняется. Не блок-лист по
# словам — тот пропускает любой ещё не придуманный вид действия (purge,
# campaign.archive, adgroups.suspend, ...) молча, а рельса обязана держать
# "никогда", а не эвристику по подстроке.
ALLOWED_ACTION_KINDS = {"bidmodifier.add", "bidmodifier.set", "schedule.set"}

# Границы почасового расписания — СВОИ, независимые от writer/schedule.py.
# Дублирование намеренное: рельса обязана считать сама, иначе она проверяет
# построитель его же формулой и пропустит любую его ошибку.
SCHEDULE_NUMBERS_PER_ROW = 25   # день недели + 24 часа
SCHEDULE_MIN = 10               # ноль запрещён отдельно: он выключает показы
SCHEDULE_MAX = 200
SCHEDULE_STEP = 10

# Путь ВОЗВРАТА — только set: он переписывает уже существующий объект в его
# прежнее значение. add на этом пути означал бы создание НОВОГО объекта вместо
# восстановления старого, то есть ещё одно изменение кабинета под видом отмены.
ROLLBACK_ALLOWED_ACTION_KINDS = {"bidmodifier.set", "schedule.set"}

# Куда обязан возвращать откат, в зависимости от вида ИСХОДНОГО действия.
# Отмена добавления — нейтраль (объекта до действия не было), отмена
# перезаписи — прежний коэффициент из previous_state журнала.
ROLLBACK_ORIGIN_ADD = "bidmodifier.add"
ROLLBACK_ORIGIN_SET = "bidmodifier.set"

_DELETE_REASON = "удаление объектов запрещено: агент только паузит"


def _is_delete(kind_lower: str) -> bool:
    return "delete" in kind_lower or "remove" in kind_lower


def check_action(action: Dict[str, Any]) -> Tuple[bool, str]:
    """Проверка одного действия. Возвращает (можно ли, причина отказа)."""
    kind = str(action.get("action_kind") or "")
    kind_lower = kind.lower()

    # Отдельная явная проверка поверх allow-листа — не для защиты (её уже
    # даёт allow-лист), а чтобы в журнале была понятная причина отказа
    # именно "удаление", а не общая "вне allow-листа".
    if _is_delete(kind_lower):
        return False, _DELETE_REASON

    if kind not in ALLOWED_ACTION_KINDS:
        return False, f"вид действия вне allow-листа: {kind}"

    percent = action.get("payload", {}).get("BidModifier")
    if percent is not None:
        if abs(int(percent)) > MODIFIER_CAP:
            return False, f"потолок корректировки ±{MODIFIER_CAP}%, получено {percent}%"

    if kind == "schedule.set":
        ok, reason = _check_schedule(action)
        if not ok:
            return False, reason
    return True, ""


def _check_schedule(action: Dict[str, Any]) -> Tuple[bool, str]:
    """Рельса расписания: цена ошибки здесь выше, чем у корректировки сегмента.

    Расписание правит кампанию ЦЕЛИКОМ, и ноль в нём — это не «ставка ниже», а
    «показов в этот час нет». Ошибка в построении обернулась бы не потерей
    эффективности, а выключенным трафиком, поэтому проверка стоит отдельно от
    построителя (writer/schedule.py) и считает независимо от него: рельса,
    доверяющая тому, кого проверяет, не рельса.
    """
    items = (((action.get("payload") or {}).get("TimeTargeting") or {})
             .get("Schedule") or {}).get("Items")
    if not items:
        return False, "расписание без часов: пустой Schedule.Items"

    for item in items:
        parts = [p.strip() for p in str(item).split(",")]
        if len(parts) != SCHEDULE_NUMBERS_PER_ROW:
            return False, (f"строка расписания обязана нести день недели и 24 часа, "
                           f"получено полей: {len(parts)}")
        try:
            numbers = [int(p) for p in parts]
        except ValueError:
            return False, f"нечисловое значение в расписании: {item!r}"

        day, hours = numbers[0], numbers[1:]
        if not 1 <= day <= 7:
            return False, f"день недели вне 1..7: {day}"
        for hour_value in hours:
            if hour_value == 0:
                return False, ("ноль в расписании выключает показы в этот час — "
                               "остановку трафика агент не назначает")
            if not SCHEDULE_MIN <= hour_value <= SCHEDULE_MAX:
                return False, (f"коэффициент расписания вне {SCHEDULE_MIN}..{SCHEDULE_MAX}: "
                               f"{hour_value}")
            if hour_value % SCHEDULE_STEP:
                return False, (f"коэффициент расписания обязан быть кратен "
                               f"{SCHEDULE_STEP}: {hour_value}")
    return True, ""


def expected_rollback_coefficient(
    origin_action_kind: Any, previous_state: Any
) -> Tuple[Optional[int], str]:
    """Коэффициент, в который откат ОБЯЗАН вернуть объект, — из журнала.

    Выводится здесь, а не принимается готовым от вызывающего кода: смысл
    сверки в том, чтобы рельса считала ожидание НЕЗАВИСИМО от построителя
    запроса (rollback.rollback_payload). Получи она ожидание из того же
    источника, что и сам запрос, — сверяла бы код сам с собой.

    Отмена добавления возвращает нейтраль (объекта до действия не было),
    отмена перезаписи — прежний коэффициент. previous_state хранится в
    ДЕЛЬТАХ, как весь внутренний план, поэтому переводится в 100-базу API.

    None вместо числа — «ожидание не выводится»: вид исходного действия
    неизвестен или прошлое состояние нечитаемо. Это отказ, а не исключение:
    вызывающий код превращает исключение в пометку «неоткатываемо навсегда».
    """
    origin = str(origin_action_kind or "")
    previous = previous_state if isinstance(previous_state, dict) else {}

    if origin == ROLLBACK_ORIGIN_ADD:
        return API_NEUTRAL, ""

    if origin == ROLLBACK_ORIGIN_SET:
        percent = previous.get("percent")
        if percent is None:
            return None, "в журнале нет прошлого коэффициента (previous_state.percent)"
        try:
            return API_NEUTRAL + int(percent), ""
        except (TypeError, ValueError):
            return None, f"прошлый коэффициент нечитаем: {percent!r}"

    return None, f"вид исходного действия неизвестен рельсе: {origin or '—'}"


def check_rollback(request: Dict[str, Any]) -> Tuple[bool, str]:
    """Рельса пути ВОЗВРАТА. Проверяет вид действия и диапазон API, но не потолок.

    Потолок MODIFIER_CAP описывает, что агенту позволено НАЗНАЧАТЬ: коридор
    ±50 % — это ограничение на его собственные решения. Куда агенту позволено
    ВЕРНУТЬСЯ, эта величина не описывает вообще: прошлое значение поставил
    человек, оно уже действовало в кабинете и штатно для Директа (+80 % на
    кампании или 0 — «показы на устройстве выключены» — обычные настройки).
    Пропущенное через потолок назначения, такое возвращаемое значение
    отклонялось бы, действие помечалось неоткатываемым навсегда, и изменение
    агента оставалось бы в кабинете вечно — то есть рельса, поставленная для
    защиты, сама отменяла бы третий слой защиты.

    Что на пути возврата остаётся жёстким:
      * удаление запрещено так же, как везде;
      * allow-лист уже пути назначения (только set: возврат переписывает
        существующий объект, а не создаёт новый);
      * коэффициент обязан лежать в диапазоне API Директа — вне его запрос
        либо будет отклонён поэлементно, либо применит не то, что задумано;
      * коэффициент обязан СОВПАДАТЬ с прошлым состоянием из журнала (для
        отмены добавления — с нейтралью). Без этой сверки рельса проверяла бы
        только форму: любое значение внутри диапазона API — а это 0..1300, то
        есть почти что угодно, — проходило бы насквозь, и единственным, что
        связывает запрос с реальным прошлым значением, оставался бы сам код,
        который этот запрос строит. Ошибка в нём (предельное значение вместо
        прежнего, чужой previous_state, потерянный знак дельты) шла бы прямо в
        боевой кабинет: путь возврата больше ничем не проверен.

    Коэффициент здесь — в 100-БАЗНОЙ ШКАЛЕ API (api_coefficient), а не в
    дельтах, как у check_action. Поле названо иначе намеренно: две рельсы
    считают в разных единицах, и одноимённое поле рано или поздно передали бы
    не в ту функцию молча.

    Отказ всегда возвращается парой (False, причина) и никогда не летит
    исключением: вызывающий код (agent_e1_watchdog.rollback_one) трактует
    исключение на этом участке как «запрос не строится» и хоронит действие
    пометкой permanent=True — то есть падение рельсы стоило бы дороже отказа.
    """
    kind = str(request.get("action_kind") or "")
    if _is_delete(kind.lower()):
        return False, _DELETE_REASON

    if kind not in ROLLBACK_ALLOWED_ACTION_KINDS:
        return False, f"вид действия вне allow-листа возврата: {kind}"

    if kind == "schedule.set":
        # У расписания возврат не описывается одним коэффициентом: назад едет
        # весь блок TimeTargeting. Сверять его с прошлым состоянием здесь
        # незачем — строитель возврата (rollback_payload) берёт блок прямо из
        # журнала и не собирает его заново, поэтому «вернуть не туда» тут
        # невозможно по построению. Требовать api_coefficient — значит
        # запретить откат расписания вовсе.
        return True, ""

    coefficient = request.get("api_coefficient")
    if coefficient is None:
        return False, "коэффициент возврата не задан"
    try:
        value = int(coefficient)
    except (TypeError, ValueError):
        return False, f"коэффициент возврата не число: {coefficient!r}"
    if not (API_MIN <= value <= API_MAX):
        return False, (f"коэффициент возврата {value} вне диапазона Директа "
                       f"{API_MIN}..{API_MAX}")

    expected, why = expected_rollback_coefficient(
        request.get("origin_action_kind"), request.get("previous_state"))
    if expected is None:
        return False, f"куда возвращать — неизвестно: {why}"
    if value != expected:
        return False, (f"коэффициент возврата {value} не равен прошлому состоянию "
                       f"журнала {expected}: это не отмена, а новое изменение")
    return True, ""


def check_holdout(
    actions: List[Dict[str, Any]], holdout_ids: Set[Any]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Разделяет действия на разрешённые и заблокированные заповедником."""
    holdout_ids = {str(h) for h in holdout_ids}
    allowed = [a for a in actions if str(a.get("object_id")) not in holdout_ids]
    blocked = [a for a in actions if str(a.get("object_id")) in holdout_ids]
    return allowed, blocked


def cap_actions(
    actions: List[Dict[str, Any]], max_per_run: int = MAX_ACTIONS_PER_RUN
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Ограничивает объём одного прогона. Остальное ждёт следующего."""
    return actions[:max_per_run], actions[max_per_run:]
