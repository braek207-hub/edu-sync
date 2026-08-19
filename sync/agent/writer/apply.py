# -*- coding: utf-8 -*-
"""
sync/agent/writer/apply.py — применение действий.

Порядок обязателен: сначала запись в журнал с прошлым состоянием, потом отправка.
Если процесс упадёт между ними, действие останется в статусе planned и будет
видно; если бы порядок был обратным, изменение в кабинете оказалось бы без следа
и без возможности отката.

Идемпотентность: действие с уже применённым ключом пропускается. Повторный
прогон в тот же день не отправляет запрос второй раз.
"""

from typing import Any, Dict, List, Tuple

# key корректировки → форма API. Проверено probe (задача 1).
_DEMOGRAPHIC_KEYS = {"GENDER_MALE", "GENDER_FEMALE"}


def to_api_call(action: Dict[str, Any]) -> Tuple[str, str, Dict[str, Any]]:
    """Действие → (сервис, метод, параметры)."""
    kind = str(action.get("action_kind") or "")
    payload = action.get("payload") or {}

    if kind == "bidmodifier.set":
        return "bidmodifiers", "set", {
            "BidModifiers": [{"Id": payload["Id"], "BidModifier": int(payload["BidModifier"])}]
        }

    if kind == "bidmodifier.add":
        item: Dict[str, Any] = {"CampaignId": int(payload["CampaignId"])}
        percent = int(payload["BidModifier"])
        direct_type = payload.get("Type")
        key = str(payload.get("key") or "")

        if direct_type == "MOBILE_ADJUSTMENT":
            item["MobileAdjustment"] = {"BidModifier": percent}
        elif direct_type == "DEMOGRAPHICS_ADJUSTMENT":
            adjustment: Dict[str, Any] = {"BidModifier": percent}
            if key in _DEMOGRAPHIC_KEYS:
                adjustment["Gender"] = key
            else:
                adjustment["Age"] = key
            item["DemographicsAdjustments"] = [adjustment]
        elif direct_type == "REGIONAL_ADJUSTMENT":
            item["RegionalAdjustments"] = [{"RegionId": int(key), "BidModifier": percent}]
        else:
            raise ValueError(f"неизвестный тип корректировки: {direct_type}")
        return "bidmodifiers", "add", {"BidModifiers": [item]}

    raise ValueError(f"неизвестный вид действия: {kind}")


def apply_actions(client, actions: List[Dict[str, Any]], db_module) -> Dict[str, Any]:
    """Применяет действия по одному: журнал → отправка → отметка результата."""
    applied = skipped = failed = 0
    details: List[Dict[str, Any]] = []

    for action in actions:
        existing = db_module.find_action_by_key(action["idempotency_key"])
        if existing and existing.get("status") in {"applied", "rolled_back"}:
            skipped += 1
            details.append({"key": action["idempotency_key"], "result": "skipped"})
            continue

        action_id = db_module.insert_action(action)
        try:
            service, method, params = to_api_call(action)
            response = client.mutate(service, method, params)
            status = "dry_run" if response.get("dry_run") else "applied"
            db_module.mark_action(action_id, status, response)
            applied += 1 if status == "applied" else 0
            details.append({"key": action["idempotency_key"], "result": status})
        except Exception as exc:
            db_module.mark_action(action_id, "failed", {"error": f"{type(exc).__name__}: {exc}"[:400]})
            failed += 1
            details.append({"key": action["idempotency_key"], "result": "failed",
                            "error": str(exc)[:200]})

    return {"applied": applied, "skipped": skipped, "failed": failed, "details": details}
