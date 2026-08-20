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
from typing import Any, Dict, List


def _idempotency_key(campaign_id: str, direct_type: str, key: str, percent: int) -> str:
    raw = f"bidmod:{campaign_id}:{direct_type}:{key}:{percent}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def diff_modifiers(
    desired: List[Dict[str, Any]], actual: List[Dict[str, Any]], campaign_id: str
) -> List[Dict[str, Any]]:
    """Действия, которых не хватает, чтобы факт совпал с планом."""
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

        actions.append({
            "action_kind": action_kind,
            "object_level": "campaign",
            "object_id": str(campaign_id),
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
        })
    return actions
