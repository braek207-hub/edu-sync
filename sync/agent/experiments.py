# -*- coding: utf-8 -*-
"""
sync/agent/experiments.py — реестр гипотез: ставка живёт как сущность.

Что чинит. До этого модуля таблица edu_agent_experiments была ПОСМЕРТНЫМ
журналом: сторож писал туда исход уже случившегося действия
(agent_e1_watchdog.action_experiment), а сам по себе замысел нигде не жил —
ни статуса, ни горизонта, ни критерия успеха, ни связи «гипотеза → действие →
замер → вердикт». Из-за этого «сразу увидеть результат и сразу двигаться
дальше» невозможно технически: возвращаться не к чему.

Здесь ход прямой: ставка заводится ДО отправки действия в кабинет и живёт до
вердикта. Идентификатор ставки выводится из идентификатора действия
(experiment_id_for), поэтому посмертная запись сторожа ложится В ТУ ЖЕ СТРОКУ,
а не заводит вторую: замысел и его исход — один объект, а не два документа,
которые потом надо сшивать.

Откуда деньги. Третьего кармана здесь нет и заводить его нельзя. Ставку
оплачивают два уже существующих механизма, и реестр только записывает
СПИСАННОЕ ими:

  * разведочный карман расчёта (portfolio.exploration_bonus, доля
    explore_share бюджета кабинета) назначает рубли кампании — они едут в
    params под ключом exploration_rub;
  * недельный риск-бюджет движка записи (writer/risk.fit_into_budget)
    списывает цену конкретного изменения — это и есть stake_rub.

Отсюда правило stake_of: действие без списанной цены ставкой не становится.
Гипотеза, которая сама назначает себе бюджет, — и есть тот третий источник.

Кто судит. Исход считает сторож (agent_e1_watchdog.economic_outcome) —
единственное место, где живёт правило «доливка обещала объём, сокращение
обещало цену». Реестр его НЕ пересчитывает, а переводит в свой статус
(WINNING_VERDICTS). Два независимых судьи разошлись бы на первом же
пограничном случае, и разошлись бы молча.
"""

import hashlib
from copy import deepcopy
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional


STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_WON = "won"
STATUS_LOST = "lost"
STATUS_ROLLED_BACK = "rolled_back"

# Открытые статусы — те, по которым реестр ещё обязан вернуться. Кортеж, а не
# множество: он уезжает параметром в SQL (db.load_open_hypotheses).
OPEN_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)
CLOSED_STATUSES = (STATUS_WON, STATUS_LOST, STATUS_ROLLED_BACK)

# Законные переходы. Записаны явно и целиком, потому что недопустимый переход
# — это не «странное состояние», а потерянная ставка: закрытая гипотеза,
# ожившая как running, второй раз спишет риск и второй раз попадёт в замер.
#
# queued → rolled_back законен: один проход сторожа может увидеть сразу и
# применение, и пробитую красную линию, и промежуточный running в этом случае
# был бы выдумкой.
# queued → lost законен: действие может не доехать до кабинета вовсе, и такая
# ставка обязана закрываться, а не висеть в очереди вечно.
LEGAL_TRANSITIONS: Dict[str, frozenset] = {
    STATUS_QUEUED: frozenset({STATUS_RUNNING, STATUS_LOST, STATUS_ROLLED_BACK}),
    STATUS_RUNNING: frozenset({STATUS_WON, STATUS_LOST, STATUS_ROLLED_BACK}),
    STATUS_WON: frozenset(),
    STATUS_LOST: frozenset(),
    STATUS_ROLLED_BACK: frozenset(),
}

# Исходы сторожа, которые считаются выигрышем ставки. Только «improved»:
# «inconclusive» и «unknown» означают, что ответа не куплено, и записывать их
# победой значит наполнять контур решений шумом — ровно тем, ради чего
# конвейер и разделён на два контура (docs/AGENT-EXPERIMENT-PIPELINE.md).
#
# Отсюда же смысл LOST: «гипотеза не подтвердилась», а не «навредила». Вред
# отличается от неопределённости не статусом, а колонками verdict и effect,
# куда сторож кладёт градуированный исход.
WINNING_VERDICTS = frozenset({"improved"})

# Горизонт замера, дней. Объявлен здесь, а не в сторожевом прогоне, потому что
# горизонт — свойство САМОЙ СТАВКИ: он назначается в момент запуска и уезжает
# в строку реестра (horizon_days). Сторож берёт это же число как длину окна
# наблюдения (agent_e1_watchdog.OBSERVATION_HORIZON_DAYS) — второй константы
# быть не должно, иначе ставка судится не по тому сроку, о котором
# договорились при запуске.
HORIZON_DAYS = 14

# Чем реестр объявляет источник денег. Строка одна на все ставки, потому что
# карман у них один и тот же; она лежит в колонке stake_source, чтобы
# появление ВТОРОГО источника было видно в данных, а не только в диффе.
STAKE_SOURCE = "explore_share+risk_budget"

# Поля строки, общие с посмертной записью сторожа (action_experiment): реестр
# заполняет их сразу, чтобы открытая ставка была читаемой строкой истории, а
# не полупустым скелетом. Сторож их уточняет при закрытии — механизм
# становится did_holdout, а класс A, если нашёлся контроль.
OPEN_MECHANISM = "before_after"
OPEN_RELIABILITY_CLASS = "B"
METRIC = "eff_cpl"
SOURCE = "bet"


class IllegalTransition(ValueError):
    """Переход статуса, которого в жизненном цикле нет."""


class StakeNotCharged(ValueError):
    """Ставка не оплачена ни одним из существующих карманов."""


def check_transition(current: str, target: str) -> None:
    """Проверяет переход. Недопустимый — исключение, а не тихий no-op.

    Тихий отказ здесь опаснее падения: ставка осталась бы в прежнем статусе,
    прогон отчитался бы об успехе, и расхождение всплыло бы через две недели
    на разборе, когда восстановить причину уже нечем.
    """
    allowed = LEGAL_TRANSITIONS.get(str(current))
    if allowed is None:
        raise IllegalTransition(f"неизвестный статус ставки: {current!r}")
    if str(target) not in allowed:
        raise IllegalTransition(
            f"переход {current!r} → {target!r} не предусмотрен; "
            f"из {current!r} допустимы: {', '.join(sorted(allowed)) or 'ничего'}")


def experiment_id_for(action_id: str) -> str:
    """Идентификатор ставки по идентификатору действия.

    Одна функция на оба конца: реестр заводит строку ДО отправки, сторож
    дописывает в неё исход ПОСЛЕ замера. Разойдись эти две формулы — и у
    каждой ставки оказалось бы две строки: открытая навсегда и закрытая
    ниоткуда.
    """
    return hashlib.sha256(f"action:{action_id}".encode("utf-8")).hexdigest()[:24]


def is_bet(action: Dict[str, Any]) -> bool:
    """Действие — ставка, если его оплатил разведочный карман.

    Признак ставится расчётом (portfolio._apply_exploration) и доезжает до
    действия в payload (writer/budget). Именно у разведочного сдвига снят
    гейт уверенности — то есть он и есть покупка информации, а не решение по
    доказательству.
    """
    payload = action.get("payload") or {}
    return bool(payload.get("exploration"))


def stake_of(action: Dict[str, Any]) -> float:
    """Ставка в рублях = риск, СПИСАННЫЙ за это действие риск-бюджетом.

    Число берётся из действия, а не считается заново: его уже посчитал и
    списал writer/risk.fit_into_budget, и второй расчёт был бы вторым
    источником правды о цене. Нет списания — нет ставки: пустое значение
    здесь означало бы бесплатную гипотезу.
    """
    raw = action.get("risk_rub")
    if raw is None:
        raise StakeNotCharged(
            f"действие {action.get('idempotency_key')!r} не прошло риск-бюджет: "
            "ставка без списанной цены не заводится")
    return round(float(raw), 2)


def success_criterion_for(action: Dict[str, Any]) -> str:
    """Критерий успеха словами — то, что сторож проверит на горизонте.

    Словами, а не формулой: считать исход здесь значило бы завести второго
    судью рядом с economic_outcome. Строка нужна человеку на разборе — чтобы
    вердикт можно было сверить с тем, о чём договаривались при запуске, не
    поднимая код сторожа.
    """
    ceiling = (action.get("red_line") or {}).get("max_value")
    ceiling_text = f"{round(float(ceiling), 2)} ₽" if ceiling else "потолка красной линии"
    expected = (action.get("payload") or {}).get("expected_leads_delta")
    if expected is not None and float(expected) > 0:
        return f"эффективных лидов больше базового темпа при CPA не выше {ceiling_text}"
    return f"CPA к концу горизонта не выше {ceiling_text}"


def _as_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def open_bet(action: Dict[str, Any], action_id: str, today: Any,
             horizon_days: int = HORIZON_DAYS) -> Dict[str, Any]:
    """Строка реестра для ставки, ЕЩЁ НЕ отправленной в кабинет.

    Четыре обязательных вещи спеки собраны здесь и все — до отправки:
    ставка списана (stake_rub), красная линия задана (red_line — копией, а не
    ссылкой: строка действия перепланировкой переписывается, а ставка обязана
    помнить линию на момент запуска), горизонт назначен (horizon_days),
    критерий успеха записан (success_criterion). Откат готовит движок записи —
    previous_state лежит в журнале действий, и дублировать его сюда нечего.
    """
    started = _as_date(today)
    return {
        "experiment_id": experiment_id_for(action_id),
        "hypothesis_type": str(action.get("action_kind") or "unknown"),
        "object_level": str(action.get("object_level") or "campaign"),
        "object_id": str(action.get("object_id")),
        "params": {
            "action_id": action_id,
            "direct_type": action.get("direct_type"),
            "setting_key": action.get("key"),
            # Рубли разведочного кармана — вторая половина ответа «откуда
            # деньги»: stake_rub говорит, во сколько оценён риск, а это —
            # сколько бюджета кампании карман на гипотезу назначил.
            "exploration_rub": (action.get("payload") or {}).get("exploration_rub"),
            "expected_leads_delta": (action.get("payload") or {}).get(
                "expected_leads_delta"),
            "risk_basis": action.get("risk_basis"),
        },
        "mechanism": OPEN_MECHANISM,
        "started_on": started.isoformat() if started else None,
        "metric": METRIC,
        "reliability_class": OPEN_RELIABILITY_CLASS,
        "source": SOURCE,
        "status": STATUS_QUEUED,
        "status_reason": "ставка заведена до отправки действия",
        "stake_rub": stake_of(action),
        "stake_source": STAKE_SOURCE,
        "horizon_days": int(horizon_days),
        "success_criterion": success_criterion_for(action),
        # Копия, а не ссылка: строка действия перепланировкой переписывается
        # (writer/db.INSERT_ACTION_SQL), а ставка обязана помнить линию на
        # момент запуска. deepcopy — потому что линия вложенная.
        "red_line": deepcopy(action.get("red_line") or {}),
        "idempotency_key": str(action.get("idempotency_key")),
    }


def _move(row: Dict[str, Any], target: str, reason: str) -> Dict[str, Any]:
    current = str(row.get("status") or "")
    check_transition(current, target)
    return {
        "experiment_id": str(row.get("experiment_id")),
        "from_status": current,
        "status": target,
        "reason": reason,
    }


def horizon_passed(row: Dict[str, Any], today: Any) -> bool:
    """Срок замера наступил.

    Отсчёт — от дня ПРИМЕНЕНИЯ, если он есть, и только иначе от дня заведения
    ставки: наблюдение начинается тогда, когда изменение оказалось в кабинете,
    а не тогда, когда его задумали. Обычно это один и тот же день, но
    отложенное риск-бюджетом действие уезжает на следующий прогон, и отсчёт
    от заведения украл бы у него часть горизонта.
    """
    start = _as_date(row.get("applied_at")) or _as_date(row.get("started_on"))
    if start is None:
        return False
    horizon = int(row.get("horizon_days") or HORIZON_DAYS)
    return _as_date(today) >= start + timedelta(days=horizon)


def settle(row: Dict[str, Any], today: Any) -> Optional[Dict[str, Any]]:
    """Что сделать со ставкой сегодня. None — ждать дальше.

    Строка приходит из db.load_open_hypotheses: поля реестра плюс состояние
    СВОЕГО действия из журнала. Порядок проверок — от необратимого к
    ожидаемому: пробитая линия закрывает ставку досрочно и перебивает любой
    вердикт по горизонту, потому что откат уже случился и мерить дальше
    нечего.
    """
    if row.get("rolled_back_at") or row.get("harmful_verdict_at"):
        return _move(row, STATUS_ROLLED_BACK,
                     "красная линия пробита — ставка снята досрочно")

    verdict = row.get("observation_verdict")
    if verdict:
        target = (STATUS_WON if str(verdict) in WINNING_VERDICTS
                  else STATUS_LOST)
        return _move(row, target, f"горизонт закрыт вердиктом «{verdict}»")

    if row.get("observation_closed_at"):
        # Наблюдение закрыто, а вердикта нет — данных на суждение не хватило.
        # Деньги потрачены, ответ не куплен: это проигрыш ставки, и прятать
        # его в «ещё наблюдаем» значило бы держать открытым то, что уже
        # никогда не закроется.
        return _move(row, STATUS_LOST,
                     "наблюдение закрыто без вердикта: ответ не куплен")

    if str(row.get("status")) == STATUS_QUEUED and row.get("applied_at"):
        return _move(row, STATUS_RUNNING, "действие применено в кабинете")

    if not row.get("applied_at") and horizon_passed(row, today):
        # Действие так и не доехало до кабинета (отклонено API, исчерпало
        # попытки, снято рельсой). Риск-бюджет за него не списан — карман
        # свободен, — но сама ставка обязана закрыться, иначе очередь реестра
        # копит замыслы, которых уже нет.
        return _move(row, STATUS_LOST,
                     "горизонт истёк, действие в кабинет не доехало")

    return None
