# -*- coding: utf-8 -*-
"""
sync/agent/writer/diff.py — разница между желаемым и фактическим состоянием.

Только add и set. Удаления нет и не будет: в Директе почти всё обратимо,
а право удалять ничего не добавляет к возможностям агента при бесконечном
хвосте риска. Лишняя корректировка, которой нет в плане, не трогается —
её мог поставить человек.

previous_state заполняется ДО применения: без него откат невозможен. Поэтому
же факт, помеченный негодным (unusable — в ответе API не было коэффициента),
не порождает ни add, ни set: прошлое состояние по нему неизвестно.
"""

import hashlib
from typing import Any, Dict, List, Optional

from sync.agent.writer import expectation, exposure, schedule


def _idempotency_key(campaign_id: str, direct_type: str, key: str, percent: int) -> str:
    raw = f"bidmod:{campaign_id}:{direct_type}:{key}:{percent}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _schedule_idempotency_key(campaign_id: str, items: List[str]) -> str:
    """Ключ действия по расписанию — от СОДЕРЖИМОГО, как у корректировок.

    В ключе корректировки лежит процент, здесь — весь набор часов: расписание
    применяется целиком, и «то же действие» означает «ровно тот же профиль».
    Пересчитал Э0 хоть один час — это уже другое действие, и закрытый ключ
    прошлого прогона его не отсечёт.
    """
    raw = "schedule:" + str(campaign_id) + ":" + "|".join(items)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def diff_schedule(
    desired_items: List[str], time_targeting: Dict[str, Any], campaign_id: str,
    context: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Действие по расписанию, если оно отличается от стоящего в кабинете.

    Список из нуля или одного элемента: у Директа нет способа поменять один
    час — Schedule принимается целиком, поэтому и действие ровно одно.

    previous_state несёт ВЕСЬ прежний блок TimeTargeting, а не только Items:
    откат обязан вернуть кампанию туда, откуда её вывели, вместе с праздничным
    режимом и учётом рабочих выходных. Сохрани мы одни часы — откат собрал бы
    блок заново и стёр бы соседние поля, которые настроил человек.

    context — экономика кампании (расход в день, цена лида), из которой
    считается ОЖИДАНИЕ действия (writer/expectation.py). Не передана —
    действие уезжает без обещания: выдумать курс «рубли → лиды» здесь не из
    чего, а ноль петля обучения зачла бы сбывшимся прогнозом.
    """
    if not desired_items:
        return []
    if not schedule.schedule_changed(time_targeting, desired_items):
        return []

    payload = {
        "CampaignId": int(campaign_id),
        "TimeTargeting": schedule.time_targeting_payload(time_targeting, desired_items),
    }
    return [expectation.attach({
        "action_kind": "schedule.set",
        "object_level": "campaign",
        "object_id": str(campaign_id),
        # Расписание двигает ставку всей кампании, поэтому доля объекта здесь
        # единица, а цену задаёт средний по суткам сдвиг (writer/exposure.py).
        "exposure": exposure.schedule_exposure(
            schedule.percent_by_hour(desired_items)),
        # Тип и ключ заполнены и здесь: по ним адресуются кулдаун после отката
        # и счётчик попыток. Без них расписание жило бы вне этих механизмов —
        # то есть переотправлялось бы вечно и после вредного вердикта.
        "direct_type": "TIME_TARGETING",
        "key": "schedule",
        "payload": payload,
        "previous_state": {"TimeTargeting": time_targeting or {}},
        "idempotency_key": _schedule_idempotency_key(campaign_id, desired_items),
    }, context)]


def diff_modifiers(
    desired: List[Dict[str, Any]], actual: List[Dict[str, Any]], campaign_id: str,
    context: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Действия, которых не хватает, чтобы факт совпал с планом.

    context — экономика кампании для ожидания, как у diff_schedule. Доля
    сегмента к ней добавляется здесь: она своя у каждого действия, а расход и
    цена лида — общие на кампанию.
    """
    actual_by_key = {(a.get("Type"), str(a.get("key"))): a for a in actual}

    actions: List[Dict[str, Any]] = []
    for item in desired:
        current = actual_by_key.get((item["direct_type"], item["key"]))
        percent = int(item["percent"])

        if current is not None and current.get("unusable"):
            # Факт по этому объекту прочитан, но негоден (в ответе API не
            # оказалось коэффициента). Действий не будет НИ ОДНОГО, и обе
            # ветки закрыты сознательно: add создал бы в кабинете второй
            # объект поверх существующего, а set записал бы в previous_state
            # выдуманное прошлое состояние и сделал откат невозможным.
            # Счётчик негодных записей уходит в отчёт прогона.
            continue

        if current is None:
            payload = {
                "CampaignId": int(campaign_id),
                "Type": item["direct_type"],
                "key": item["key"],
                "BidModifier": percent,
            }
            previous_state: Dict[str, Any] = {}
            action_kind = "bidmodifier.add"
        elif int(current.get("percent") or 0) != percent:
            payload = {"Id": current["Id"], "BidModifier": percent}
            previous_state = {"Id": current["Id"], "percent": int(current.get("percent") or 0)}
            action_kind = "bidmodifier.set"
        else:
            continue

        actions.append(expectation.attach({
            "action_kind": action_kind,
            "object_level": "campaign",
            "object_id": str(campaign_id),
            # Цена риска этого действия: доля сегмента × сила сдвига, а не
            # расход всей кампании (writer/exposure.py). Доля приходит из
            # плана; её отсутствие честно означает «весь объект».
            "exposure": exposure.bid_modifier_exposure(percent, item.get("share")),
            # Вид и ключ корректировки — на верхнем уровне действия, а не
            # только внутри payload: у bidmodifier.set payload несёт лишь Id,
            # и без этих полей отчёт прогона не мог бы сказать, ЧТО именно
            # правится, — только «одно изменение в кампании 111».
            "direct_type": item["direct_type"],
            "key": item["key"],
            "payload": payload,
            "previous_state": previous_state,
            "idempotency_key": _idempotency_key(
                campaign_id, item["direct_type"], item["key"], percent
            ),
        }, {**(context or {}), "segment_share": item.get("share")}))
    return actions
