# -*- coding: utf-8 -*-
"""
sync/agent/build_queue.py — очередь нарядов билдеру: сторона АГЕНТА.

Наряд (sync/agent/build_order.py) описывает, ЧТО собрать. Этот модуль — как
наряд доезжает до исполнителя и как исход возвращается обратно. Граница здесь
не техническая, а собственническая: тело кампании — группы, ключи, тексты —
собирает другой репозиторий («EDU кампании», d:\\vscode\\EDU), и `campaign.create`
вне allow-листа записи не по недосмотру, а потому что из edu-sync такой запрос
невыразим (writer/guardrails.BUILDER_REASON).

**Почему таблица, а не файл и не колонка идеи.** Файл на диске невидим второму
процессу и не переживает прогона. Колонка в edu_agent_ideas отдала бы билдеру
весь реестр решений агента — он читал бы то, о чём ему знать нечего, — и
смешала бы два жизненных цикла: идея закрывается вердиктом о ЗАМЫСЛЕ, наряд —
фактом сборки. Их состояния расходятся: идея может быть уже отклонена
человеком, пока кампания по её наряду собирается.

**Наряд — очередь на исполнение, а не журнал.** Ключ строки — order_id, и он
без даты (build_order.make_order_id): у реестра ровно одна открытая идея на
(кабинет, вид, направление), и повторная находка полгода спустя — та же
консолидация с обновлённым составом фраз. Поэтому повторная постановка
ОБНОВЛЯЕТ тело наряда, пока он в очереди, и НЕ трогает его, как только билдер
его взял: собирать движущуюся цель нельзя — половина кампании оказалась бы
собрана по одному составу фраз, половина по другому, а кросс-минусовка
доноров — по третьему.

**Ответ обязан нести две вещи, и вторая важнее.** `campaign_id` — адрес
объекта: без него агент не может ни наблюдать кампанию, ни поставить её на
паузу откатом. `started_on` — день ПЕРВОЙ ОТКРУТКИ, а не день создания:
кампания создаётся на паузе (writer/launch.STATE_SUSPENDED), между созданием
и включением стоит человек, и окно наблюдения, отсчитанное от создания,
съело бы горизонт днями простоя. Кампания собрана, но ещё не включена — это
законный ответ (`built` без даты), и наблюдение по нему не заводится: мерить
нечего.

**Наблюдение — против заповедника, и другого варианта нет.** У новой кампании
нет своей истории, поэтому «до и после» физически невозможно, а разность
разностей не из чего собрать. Контроль один — заповедник (holdout.py), тот же,
которым меряется такт целиком. Отсюда механизм vs_holdout и класс
достоверности B: контроль есть, но кампания попала в опыт не жребием, а
решением агента, и назвать это A значило бы завысить доверие к вердикту.

Модуль двухслойный, как остальные: верх — чистые функции от наряда и ответа,
низ — тонкая обвязка к базе. Правила переходов живут в Python, SQL тупой.
"""

import json
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import psycopg2.extras

from sync.db import get_connection
from sync.agent import build_order, experiments
from sync.agent.writer import launch

# Жизненный цикл наряда. Записан явно и целиком: недопустимый переход — это не
# «странное состояние», а потерянная кампания. Наряд, ожившый из built обратно
# в queued, уехал бы билдеру второй раз, и в кабинете появилась бы кампания-
# близнец, делящая с первой один аукцион.
STATUS_QUEUED = "queued"        # лежит в очереди, тело ещё можно обновлять
STATUS_TAKEN = "taken"          # билдер взял; тело заморожено
STATUS_BUILT = "built"          # кампания собрана; есть campaign_id
STATUS_FAILED = "failed"        # билдер не смог; причина в status_reason
STATUS_CANCELLED = "cancelled"  # снят агентом или человеком до сборки

# Открытые статусы — те, по которым агент ещё обязан вернуться. Кортеж, а не
# множество: уезжает параметром в SQL.
OPEN_STATUSES = (STATUS_QUEUED, STATUS_TAKEN)
CLOSED_STATUSES = (STATUS_BUILT, STATUS_FAILED, STATUS_CANCELLED)

# built → built законен и нужен: билдер отвечает дважды — сначала «собрал,
# кампания на паузе» (без даты старта), потом «включили такого-то числа».
# Запрети этот переход — и наблюдение нельзя было бы завести никогда, потому
# что в момент сборки дня открутки ещё не существует.
#
# failed → queued законен: билдер не смог по устранимой причине (не хватило
# оговорок в паспорте, модерация), причину починили, наряд возвращается.
LEGAL_TRANSITIONS: Dict[str, frozenset] = {
    STATUS_QUEUED: frozenset({STATUS_TAKEN, STATUS_FAILED, STATUS_CANCELLED}),
    STATUS_TAKEN: frozenset({STATUS_BUILT, STATUS_FAILED, STATUS_CANCELLED}),
    STATUS_BUILT: frozenset({STATUS_BUILT}),
    STATUS_FAILED: frozenset({STATUS_QUEUED, STATUS_CANCELLED}),
    STATUS_CANCELLED: frozenset(),
}

# Способ замера и класс достоверности наблюдения за новой кампанией — см.
# шапку модуля. Константы здесь, а не в experiments.py, потому что там они
# описывают ставку НАД СУЩЕСТВУЮЩИМ объектом («до и после»), а у наряда
# другой случай: объекта до опыта не было вовсе.
MECHANISM = "vs_holdout"
RELIABILITY_CLASS = "B"
SOURCE = "build_order"

# Чем оплачена кампания наряда. Это НЕ третий карман рядом с разведочным и
# риск-бюджетом: недельный лимит новой кампании равен тому, что доноры по
# этим же фразам уже тратят (writer/launch.campaign_from_donors считает его
# из их измеренного расхода). Деньги переезжают, а не назначаются, и колонка
# stake_source обязана называть это прямо — иначе разбор экономики агента
# посчитал бы переезд новой тратой.
STAKE_SOURCE = "donor_spend_moved"

DAYS_IN_WEEK = 7.0


class IllegalTransition(ValueError):
    """Переход статуса наряда, которого в жизненном цикле нет."""


class OrderFrozen(ValueError):
    """Попытка переписать тело наряда, который билдер уже взял."""


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _number(value: Any) -> Optional[float]:
    try:
        if value is None or isinstance(value, bool):
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _day(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def check_transition(current: str, target: str) -> None:
    """Проверяет переход. Недопустимый — исключение, а не тихий no-op.

    Тихий отказ здесь опаснее падения: наряд остался бы в прежнем статусе,
    прогон отчитался бы об успехе, и расхождение всплыло бы недели спустя —
    когда в кабинете уже стоит кампания, о которой агент ничего не знает.
    """
    allowed = LEGAL_TRANSITIONS.get(_text(current))
    if allowed is None:
        raise IllegalTransition(f"неизвестный статус наряда: {current!r}")
    if _text(target) not in allowed:
        raise IllegalTransition(
            f"переход {current!r} → {target!r} не предусмотрен; "
            f"из {current!r} допустимы: {', '.join(sorted(allowed)) or 'ничего'}")


# ------------------------------------------------------------- постановка


def queue_row(order: Dict[str, Any]) -> Dict[str, Any]:
    """Наряд → строка очереди. Негодный наряд — ValueError с причиной.

    Валидатор зовётся ЗДЕСЬ, а не «потом у получателя»: наряд с дырой,
    доехавший до очереди, выглядел бы для человека исправным и ждал бы
    исполнения, которого ему нельзя дать. Проверка у отправителя не отменяет
    проверки у получателя — она отменяет молчаливое ожидание.

    В строку кладётся ПРОВЕРЕННАЯ копия (с нормализованными фразами), а не
    исходный словарь: билдер обязан собирать ровно то, что проверил валидатор,
    иначе проверка была о другом тексте.
    """
    checked = build_order.validate(order)
    return {
        "order_id": checked["order_id"],
        "idea_id": checked.get("idea_id"),
        "account": checked["account"],
        "kind": checked["kind"],
        "level_slug": checked["level_slug"],
        "campaign_name": checked["campaign_name"],
        "direction": checked.get("direction") or None,
        "status": STATUS_QUEUED,
        "status_reason": "наряд поставлен в очередь генератором",
        "order_json": checked,
    }


def merge_queued(existing: Optional[Dict[str, Any]],
                 fresh: Dict[str, Any]) -> Dict[str, Any]:
    """Свежая постановка поверх того, что уже лежит в очереди.

    Пока наряд в очереди — тело обновляется целиком: состав фраз плавает от
    прогона к прогону (registry.GENERATOR_FIELDS), и билдеру обязан достаться
    последний, а не тот, что нашёлся первым.

    Как только билдер его взял — тело заморожено, и попытка переписать это
    исключение, а не тихое сохранение прежнего: молчание здесь означало бы,
    что генератор считает наряд обновлённым, а собирается старый.

    Закрытый наряд (built/failed/cancelled) свежей постановкой НЕ воскресает:
    статусом распоряжается ответ билдера и решение человека, а не очередной
    прогон генератора. Возврат в очередь — отдельное действие (requeue).
    """
    if existing is None:
        return fresh
    status = _text(existing.get("status"))
    if status == STATUS_QUEUED:
        return {**existing, **fresh}
    if status == STATUS_TAKEN:
        raise OrderFrozen(
            f"наряд {fresh['order_id']!r} уже взят билдером: обновление тела "
            "означало бы, что половина кампании собрана по одному составу "
            "фраз, а кросс-минусовка доноров — по другому")
    return dict(existing)


# ------------------------------------------------------------------ ответ


def accept_row(row: Dict[str, Any], *, campaign_id: str,
               started_on: Any = None,
               note: str = "") -> Dict[str, Any]:
    """Ответ билдера «собрал» → обновление строки очереди.

    campaign_id обязателен: ответ без адреса объекта — это не «собрал», а
    «что-то произошло». Такой ответ едет через fail_row, и разница видна в
    статусе, а не в комментарии.

    started_on необязателен и означает ровно то, что написано: кампания
    создаётся на паузе, и день первой открутки в момент сборки ещё не
    существует. Отсутствие даты — не дефект ответа, а состояние кампании.
    """
    campaign = _text(campaign_id)
    if not campaign:
        raise ValueError(
            "ответ билдера без campaign_id: наблюдать и откатывать нечего — "
            "у агента нет адреса созданного объекта")
    check_transition(_text(row.get("status")), STATUS_BUILT)
    started = _day(started_on)
    return {
        **row,
        "status": STATUS_BUILT,
        "status_reason": (_text(note) or
                          ("кампания собрана и запущена" if started
                           else "кампания собрана и стоит на паузе: дня "
                                "первой открутки ещё нет")),
        "campaign_id": campaign,
        "started_on": started.isoformat() if started else None,
    }


def fail_row(row: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """Ответ билдера «не смог» → обновление строки.

    Причина обязательна и хранится строкой: наряд, закрывшийся молча,
    неотличим от наряда, до которого не дошли руки, — и следующий прогон
    поставил бы его заново, не зная, что уже спрашивал.
    """
    text = _text(reason)
    if not text:
        raise ValueError(
            "отказ билдера без причины: наряд закрылся бы молча, и следующий "
            "прогон поставил бы его заново, не зная, что уже спрашивал")
    check_transition(_text(row.get("status")), STATUS_FAILED)
    return {**row, "status": STATUS_FAILED, "status_reason": text}


# ------------------------------------------------------------ наблюдение


def observation(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Строка очереди → наблюдение в реестре гипотез. None — заводить рано.

    None означает одно из двух, и оба законны: кампания ещё не собрана, или
    собрана, но не включена. Мерить в обоих случаях нечего, а строка реестра,
    заведённая заранее, обещала бы вердикт по окну, в котором не было ни
    одного дня открутки.

    Идентификатор наблюдения выводится из КЛЮЧА ДЕЙСТВИЯ запуска, а не из
    order_id напрямую. Тем же ключом действие легло бы в журнал записи, если
    бы `campaign.create` когда-нибудь стал применимым рычагом, — и тогда
    посмертная запись сторожа попала бы в ЭТУ ЖЕ строку, а не завела вторую.
    Замысел и его исход — один объект, а не два документа.

    Метрика берётся из критерия наряда, а не из experiments.METRIC: критерий
    объявлен заранее и уехал билдеру вместе с кампанией, и судить её другой
    мерой значило бы оценивать задним числом.
    """
    if _text(row.get("status")) != STATUS_BUILT:
        return None
    started = _day(row.get("started_on"))
    campaign_id = _text(row.get("campaign_id"))
    if started is None or not campaign_id:
        return None

    order = row.get("order_json") or {}
    if isinstance(order, str):
        order = json.loads(order)
    checked = build_order.validate(order)
    action = launch.build(checked)

    rule = checked["success_rule"]
    horizon = int(checked["horizon_days"])
    weekly = float(checked["campaign"]["weekly_budget"])
    return {
        "experiment_id": experiments.experiment_id_for(
            action["idempotency_key"]),
        "hypothesis_type": launch.LAUNCH_KIND,
        "object_level": "campaign",
        "object_id": campaign_id,
        "params": {
            "order_id": checked["order_id"],
            "idea_id": checked.get("idea_id"),
            "level_slug": checked["level_slug"],
            "campaign_name": checked["campaign_name"],
            "direction": checked.get("direction"),
            # Контроль назван в самой строке, а не подразумевается: разбор
            # через квартал обязан читать «с чем сравнивали» из данных.
            "control": "holdout",
            "donor_campaign_ids": sorted(
                {item["campaign_id"] for item in checked["donor_negatives"]}),
            "window_days": checked["window_days"],
        },
        "mechanism": MECHANISM,
        "started_on": started.isoformat(),
        "metric": _text(rule.get("metric")),
        "reliability_class": RELIABILITY_CLASS,
        "source": SOURCE,
        "status": experiments.STATUS_RUNNING,
        "status_reason": "кампания наряда откручивается, идёт замер",
        "stake_rub": round(weekly / DAYS_IN_WEEK * horizon, 2),
        "stake_source": STAKE_SOURCE,
        "horizon_days": horizon,
        "success_criterion": success_criterion(checked),
        # Красной линии у наряда нет: рельсы стерегут запросы к API, а
        # кампанию создавал не агент. Пустой словарь, а не отсутствие ключа,
        # — чтобы «не читали» и «нечего читать» не выглядели одинаково.
        "red_line": {},
        "idempotency_key": action["idempotency_key"],
    }


def success_criterion(order: Dict[str, Any]) -> str:
    """Критерий успеха наряда словами — то, что проверит разбор.

    Словами, а не формулой: считать исход здесь значило бы завести второго
    судью рядом с ideas/feedback_out.report, который уже судит наряд ровно
    этим критерием. Строка нужна человеку — чтобы вердикт можно было сверить
    с тем, о чём договаривались при постановке, не поднимая код.
    """
    rule = order.get("success_rule") or {}
    metric = _text(rule.get("metric")) or "метрика"
    threshold = _number(rule.get("threshold"))
    if threshold is None:
        threshold = _number(rule.get("value"))
    base = {"vs_donors": "донорской базы",
            "vs_direction": "цены направления",
            "did_vs_holdout": "заповедника"}.get(
                _text(rule.get("comparison")), _text(rule.get("comparison")))
    horizon = order.get("horizon_days")
    return (f"{metric} не выше {threshold:g} против {base} "
            f"за {horizon} дн.")


# ---------------------------------------------------------------- база


COLUMNS = ("order_id", "idea_id", "account", "kind", "level_slug",
           "campaign_name", "direction", "status", "status_reason",
           "order_json", "campaign_id", "started_on", "experiment_id")

UPSERT_SQL = """
    INSERT INTO edu_agent_build_orders (
        order_id, idea_id, account, kind, level_slug, campaign_name,
        direction, status, status_reason, order_json, campaign_id,
        started_on, experiment_id
    ) VALUES (
        %(order_id)s, %(idea_id)s, %(account)s, %(kind)s, %(level_slug)s,
        %(campaign_name)s, %(direction)s, %(status)s, %(status_reason)s,
        %(order_json)s, %(campaign_id)s, %(started_on)s, %(experiment_id)s
    )
    ON CONFLICT (order_id) DO UPDATE SET
        idea_id       = EXCLUDED.idea_id,
        account       = EXCLUDED.account,
        kind          = EXCLUDED.kind,
        level_slug    = EXCLUDED.level_slug,
        campaign_name = EXCLUDED.campaign_name,
        direction     = EXCLUDED.direction,
        status        = EXCLUDED.status,
        status_reason = EXCLUDED.status_reason,
        order_json    = EXCLUDED.order_json,
        campaign_id   = EXCLUDED.campaign_id,
        started_on    = EXCLUDED.started_on,
        experiment_id = EXCLUDED.experiment_id,
        updated_at    = now()
"""

SELECT_ONE_SQL = "SELECT * FROM edu_agent_build_orders WHERE order_id = %s"

SELECT_BY_STATUS_SQL = """
    SELECT * FROM edu_agent_build_orders
     WHERE status = ANY(%(statuses)s)
       AND (%(account)s IS NULL OR account = %(account)s)
     ORDER BY queued_at
"""


def _params(row: Dict[str, Any]) -> Dict[str, Any]:
    """Строка → параметры запроса, ровно по объявленным колонкам.

    Проекция на COLUMNS обязательна: строка, прочитанная из базы, несёт ещё и
    отметки времени, а слияние может дописать в неё что угодно. Лишний ключ в
    базу молча не поедет, недостающий уехал бы как NULL — оба конца держит
    один список.
    """
    params = {column: row.get(column) for column in COLUMNS}
    order = row.get("order_json") or {}
    params["order_json"] = (order if isinstance(order, str)
                            else json.dumps(order, ensure_ascii=False))
    started = _day(row.get("started_on"))
    params["started_on"] = started.isoformat() if started else None
    return params


def _write(row: Dict[str, Any]) -> Dict[str, Any]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(UPSERT_SQL, _params(row))
        conn.commit()
    return row


def load(order_id: str) -> Optional[Dict[str, Any]]:
    """Строка наряда по order_id. None — наряда с таким ключом нет."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(SELECT_ONE_SQL, (_text(order_id),))
            found = cur.fetchone()
            return dict(found) if found else None


def by_status(statuses=OPEN_STATUSES,
              account: Optional[str] = None) -> List[Dict[str, Any]]:
    """Наряды в названных статусах — то, что читает билдер и экран агента."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(SELECT_BY_STATUS_SQL,
                        {"statuses": list(statuses),
                         "account": None if account is None else _text(account)})
            return [dict(r) for r in cur.fetchall()]


def enqueue(order: Dict[str, Any]) -> Dict[str, Any]:
    """Наряд → очередь. Уже взятый билдером наряд не переписывается."""
    fresh = queue_row(order)
    return _write(merge_queued(load(fresh["order_id"]), fresh))


def take(order_id: str, by: str) -> Optional[Dict[str, Any]]:
    """Билдер берёт наряд: тело замораживается. None — наряда нет."""
    row = load(order_id)
    if row is None:
        return None
    check_transition(_text(row.get("status")), STATUS_TAKEN)
    taken = {**row, "status": STATUS_TAKEN,
             "status_reason": f"наряд взят билдером: {_text(by) or 'без имени'}"}
    return _write(taken)


def accept(order_id: str, *, campaign_id: str, started_on: Any = None,
           note: str = "") -> Optional[Dict[str, Any]]:
    """Ответ билдера + заведение наблюдения. None — наряда нет.

    Две записи в одном вызове намеренно: наряд без наблюдения — это кампания,
    которую агент создал и не смотрит, а именно ради «завести наблюдение»
    ответ и нужен. Наблюдение пишется ПЕРВЫМ: упади запись строки наряда
    после него, следующий вызов повторит обе (upsert по ключу идемпотентен), а
    обратный порядок оставил бы наряд закрытым без замера.
    """
    from sync.agent import db as agent_db

    row = load(order_id)
    if row is None:
        return None
    updated = accept_row(row, campaign_id=campaign_id, started_on=started_on,
                         note=note)
    watch = observation(updated)
    if watch is not None:
        agent_db.upsert_hypotheses([watch])
        updated["experiment_id"] = watch["experiment_id"]
    return _write(updated)


def fail(order_id: str, reason: str) -> Optional[Dict[str, Any]]:
    """Отказ билдера с причиной. None — наряда нет."""
    row = load(order_id)
    if row is None:
        return None
    return _write(fail_row(row, reason))
