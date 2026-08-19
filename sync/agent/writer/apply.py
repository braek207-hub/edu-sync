# -*- coding: utf-8 -*-
"""
sync/agent/writer/apply.py — применение действий.

Порядок обязателен: сначала запись в журнал с прошлым состоянием, потом отправка.
Если процесс упадёт между ними, действие останется в статусе planned и будет
видно; если бы порядок был обратным, изменение в кабинете оказалось бы без следа
и без возможности отката.

Идемпотентность: действие с уже применённым ключом пропускается. Повторный
прогон в тот же день не отправляет запрос второй раз.

Транспорт (client.py) намеренно НЕ поднимает исключение на ошибку уровня
ЭЛЕМЕНТА (result.AddResults[]/SetResults[].Errors, например код 8800
«кампания не найдена») — она приходит в успешном HTTP-ответе и остаётся в
result для разбора здесь. Без разбора такая ошибка выглядела бы как success:
статус ушёл бы в 'applied', а на следующем прогоне идемпотентный ключ нашёлся
бы уже применённым и действие, которое физически не создалось в кабинете,
навсегда осталось бы недостижимым для повторной попытки.
"""

from typing import Any, Dict, List, Optional, Tuple

# key корректировки → форма API. Проверено probe (задача 1).
_DEMOGRAPHIC_KEYS = {"GENDER_MALE", "GENDER_FEMALE"}

# API-метод → имя коллекции результатов по элементам в ответе.
_RESULT_COLLECTION = {"add": "AddResults", "set": "SetResults"}


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


def _element_errors(method: str, response: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """Ошибки уровня элемента в успешном HTTP-ответе (result.*Results[].Errors).

    to_api_call всегда кладёт ровно один элемент в BidModifiers, поэтому
    результатов по элементам тоже ровно один — берём первый. Коллекция пуста
    (известный метод, но результатов нет) — считать нечего, возвращаем None,
    а не пустой список, чтобы вызывающий код не путал «нет данных для
    разбора» с «разобрали, ошибок нет».

    Неизвестный method — отдельный случай: это не «ошибок нет», а «ответ не
    разобран», и трактовать его как success нельзя. Ровно этот дефект уже
    ловился для add/set (отклонённое API действие уходило в 'applied' и
    навсегда застревало за детерминированным идемпотентным ключом) — для
    любого будущего вида операции без записи в _RESULT_COLLECTION вызов
    обязан упасть явно, а не молча вернуть «ошибок нет».
    """
    collection_key = _RESULT_COLLECTION.get(method)
    if not collection_key:
        raise ValueError(f"неизвестный метод для разбора ответа уровня элемента: {method}")
    items = response.get(collection_key) or []
    if not items:
        return None
    errors = (items[0] or {}).get("Errors")
    return errors or None


def apply_actions(client, actions: List[Dict[str, Any]], db_module) -> Dict[str, Any]:
    """Применяет действия по одному: журнал → отправка → отметка результата.

    Статус действия после отправки — один из четырёх, не два:
      - 'dry_run'  — mutate не уходил в API (client.dry_run=True);
      - 'applied'  — API принял запрос И элемент применился без Errors;
      - 'rejected' — API вернул 200, но ОТКЛОНИЛ элемент (Errors в
                     AddResults/SetResults, например 8800 «кампания не
                     найдена»). Это не 'failed' — запрос состоялся, ответ
                     разобран; и не 'applied' — в кабинете ничего не
                     изменилось. Не входит в набор {'applied','rolled_back'},
                     который блокирует повтор по идемпотентности, поэтому
                     отклонённое действие ОБЯЗАНО переприменяться на
                     следующем прогоне, а не пропускаться;
      - 'failed'   — исключение при отправке (сеть, 5xx после ретраев,
                     error уровня запроса) ИЛИ ответ пришёл, но разобрать
                     его нечем (_element_errors не знает метод — новый вид
                     операции без записи в _RESULT_COLLECTION). Оба случая
                     переприменяются на следующем прогоне по той же причине:
                     неразобранный ответ — отказ по умолчанию, а не 'applied'.
    """
    applied = skipped = failed = rejected = 0
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
            if response.get("dry_run"):
                status = "dry_run"
            else:
                errors = _element_errors(method, response)
                status = "rejected" if errors else "applied"
            db_module.mark_action(action_id, status, response)
            applied += 1 if status == "applied" else 0
            rejected += 1 if status == "rejected" else 0
            details.append({"key": action["idempotency_key"], "result": status})
        except Exception as exc:
            db_module.mark_action(action_id, "failed", {"error": f"{type(exc).__name__}: {exc}"[:400]})
            failed += 1
            details.append({"key": action["idempotency_key"], "result": "failed",
                            "error": str(exc)[:200]})

    return {"applied": applied, "skipped": skipped, "failed": failed, "rejected": rejected,
            "details": details}
