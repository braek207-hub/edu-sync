# -*- coding: utf-8 -*-
"""
sync/agent/drift.py — сверка журнала с кабинетом.

Журнал говорит «применено»: API вернул успех, строка закрылась статусом
applied. Это доказательство того, что письмо отправлено, а не того, что его
прочитали. Правда об изменении живёт в кабинете, и между ней и журналом
есть щель:

  • кто-то из людей вернул настройку руками — агент об этом не знает и
    продолжает считать, что его изменение работает; наблюдение выносит
    вердикт «сработало / не сработало» по периоду, в котором изменения уже
    не было, и петля обучения учится на выдумке;
  • API принял вызов и не применил его целиком (пакетная стратегия,
    конфликт настроек, автоматика Директа) — снаружи это выглядит успехом;
  • кампания удалена или архивирована, а действия по ней всё ещё судятся.

Все три случая невидимы изнутри агента: он сверяет себя с собственным
журналом. Поэтому сверка читает кабинет заново и сравнивает три величины —
ожидание (payload), точку возврата (previous_state) и факт. Совпадение с
точкой возврата — отдельный вердикт: это не «разъехалось на пару процентов»,
это откат, у него есть автор и его надо искать.

Модуль чистый: он ничего не читает и не пишет — только сравнивает
прочитанное. Ходит по API и в базу sync/agent_drift.py.
"""

from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from sync.agent.writer.budget import read_weekly_limit
from sync.agent.writer.tcpa import read_target_cpa

MATCH = "match"
# Значение уехало и от ожидания, и от точки возврата: его двигал кто-то
# третий или Директ округлил сильнее допуска.
DRIFTED = "drifted"
# Значение вернулось ровно в точку возврата — это откат, а не дрейф.
REVERTED = "reverted"
OBJECT_GONE = "object_gone"
# Вид действия, для которого сверка ещё не написана. Отдельный вердикт, а не
# молчаливый пропуск: «не проверено» обязано отличаться от «проверено и
# сошлось», иначе покрытие сверки нельзя измерить.
UNVERIFIABLE = "unverifiable"
UNREADABLE = "unreadable"

# Директ округляет микрорубли и хранит недельный лимит с точностью до
# копеек стратегии. Допуск относительный: полпроцента — это заведомо меньше
# любого шага, которым ходят рычаги (минимальный шаг — единицы процентов).
TOLERANCE = 0.005


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dig(source: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(source, dict):
            return None
        source = source.get(key)
    return source


def _weekly(payload: Dict[str, Any]) -> Any:
    return _dig(payload, "WeeklySpendLimit")


def _weekly_actual(state: Dict[str, Any]) -> Any:
    _, micros, _ = read_weekly_limit(state.get("strategy") or {})
    return micros


def _daily(payload: Dict[str, Any]) -> Any:
    return _dig(payload, "DailyBudget", "Amount")


def _daily_actual(state: Dict[str, Any]) -> Any:
    return _dig(state, "daily_budget", "Amount")


def _tcpa(payload: Dict[str, Any]) -> Any:
    return _dig(payload, "TargetCpa")


def _tcpa_actual(state: Dict[str, Any]) -> Any:
    return read_target_cpa(state.get("strategy") or {})


SUSPENDED = "SUSPENDED"


def _state_expected(payload: Dict[str, Any]) -> Any:
    # У выключения величины в payload нет — там только CampaignId. Ожидание
    # известно из самого вида действия, и это единственный случай, когда его
    # законно взять не из полезной нагрузки.
    return SUSPENDED


def _state_actual(state: Dict[str, Any]) -> Any:
    return state.get("state")


def _state_previous(previous: Dict[str, Any]) -> Any:
    return _dig(previous, "State")


# Вид действия → (что ожидали, что стоит сейчас, куда возвращаться).
# Третий элемент читает previous_state: у бюджета и цели поле называется так
# же, как в payload, у выключателя — иначе, и справочник это скрывает.
READERS: Dict[str, Tuple[Callable, Callable, Callable]] = {
    "WEEKLY_SPEND_LIMIT": (_weekly, _weekly_actual, _weekly),
    "DAILY_BUDGET": (_daily, _daily_actual, _daily),
    "AVERAGE_CPA": (_tcpa, _tcpa_actual, _tcpa),
    "CAMPAIGN_STATE": (_state_expected, _state_actual, _state_previous),
}


def same(left: Any, right: Any, tolerance: float = TOLERANCE) -> bool:
    """Равны ли две величины с поправкой на округление Директа."""
    if left is None or right is None:
        return False
    a, b = _num(left), _num(right)
    if a is None or b is None:
        return str(left).strip().upper() == str(right).strip().upper()
    if a == b:
        return True
    scale = max(abs(a), abs(b))
    return scale > 0 and abs(a - b) <= tolerance * scale


def latest_per_segment(actions: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Из истории по сегменту оставляет самое позднее применённое действие.

    Иначе сверка объявила бы дрейфом собственную работу агента: вчерашнее
    изменение сегодня перекрыто сегодняшним, и кабинет обязан показывать
    последнее. Порядок входа сохраняется — первым идёт тот сегмент, который
    встретился первым.
    """
    best: Dict[Tuple, Dict[str, Any]] = {}
    order: List[Tuple] = []
    for action in actions or ():
        if not isinstance(action, dict):
            continue
        key = (str(action.get("account") or ""), str(action.get("object_id") or ""),
               str(action.get("direct_type") or ""), str(action.get("setting_key")
                                                         or action.get("key") or ""))
        if key not in best:
            order.append(key)
            best[key] = action
            continue
        if str(action.get("applied_at") or "") > str(best[key].get("applied_at") or ""):
            best[key] = action
    return [best[key] for key in order]


def check(action: Dict[str, Any],
          state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Одно применённое действие + состояние кабинета → вердикт сверки."""
    kind = str(action.get("direct_type") or "")
    out: Dict[str, Any] = {
        "action_id": action.get("action_id"),
        "account": action.get("account"),
        "object_id": str(action.get("object_id") or ""),
        "direct_type": kind,
        "key": action.get("setting_key") or action.get("key") or "",
        "applied_at": str(action.get("applied_at") or "") or None,
        "expected": None, "actual": None, "previous": None,
    }
    readers = READERS.get(kind)
    if readers is None:
        out["verdict"] = UNVERIFIABLE
        return out
    if not state:
        out["verdict"] = OBJECT_GONE
        return out

    expected_of, actual_of, previous_of = readers
    payload = action.get("payload") or {}
    previous_state = action.get("previous_state") or {}
    expected = expected_of(payload)
    actual = actual_of(state)
    previous = previous_of(previous_state)
    out.update({"expected": expected, "actual": actual, "previous": previous})

    if expected is None or actual is None:
        # Нечитаемое ожидание и нечитаемый факт — разные новости, но обе
        # означают «сверка не состоялась», и выдавать их за совпадение
        # нельзя: молчаливое «сошлось» здесь опаснее шумного «не знаю».
        out["verdict"] = UNREADABLE
        return out
    if same(actual, expected):
        out["verdict"] = MATCH
        return out
    # Порядок важен: сначала проверяется точка возврата. Значение, равное и
    # ожиданию, и точке возврата, сюда не доходит — такое действие ничего не
    # меняло и до кабинета не добралось бы.
    if previous is not None and same(actual, previous):
        out["verdict"] = REVERTED
        return out
    out["verdict"] = DRIFTED
    return out


def summarize(rows: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for row in rows or ():
        verdict = str(row.get("verdict") or UNREADABLE)
        out[verdict] = out.get(verdict, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# Вердикты, требующие человека. Дрейф и откат — не сбой прогона, а сообщение
# о том, что кабинет живёт своей жизнью: агент судит исходы по изменениям,
# которых там уже нет.
ALARMING = (REVERTED, DRIFTED, OBJECT_GONE)


def alarms(rows: Iterable[Dict[str, Any]]) -> List[str]:
    counts = summarize(rows)
    return [f"{verdict}: {counts[verdict]}" for verdict in ALARMING
            if counts.get(verdict)]
