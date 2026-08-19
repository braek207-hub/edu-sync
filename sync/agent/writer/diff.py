# -*- coding: utf-8 -*-
"""
sync/agent/writer/diff.py — разница между желаемым и фактическим состоянием.

Только add и set. Удаления нет и не будет: в Директе почти всё обратимо,
а право удалять ничего не добавляет к возможностям агента при бесконечном
хвосте риска. Лишняя корректировка, которой нет в плане, не трогается —
её мог поставить человек.

previous_state заполняется ДО применения: без него откат невозможен.
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
            "payload": payload,
            "previous_state": previous_state,
            "idempotency_key": _idempotency_key(
                campaign_id, item["direct_type"], item["key"], percent
            ),
        })
    return actions
