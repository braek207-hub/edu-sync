# -*- coding: utf-8 -*-
"""
sync/agent/writer/apply.py — применение действий.

Порядок обязателен: сначала запись в журнал с прошлым состоянием, потом отправка.
Если процесс упадёт между ними, действие останется в статусе planned и будет
видно; если бы порядок был обратным, изменение в кабинете оказалось бы без следа
и без возможности отката.

Идемпотентность: действие с ключом в закрытом статусе (db.FINAL_STATUSES —
applied / rolled_back / stale) пропускается. Повторный прогон в тот же день не
отправляет запрос второй раз. Набор статусов берётся из того же кортежа, что
стоит в условии ON CONFLICT журнала: «кого не переписываем» и «кого не
отправляем второй раз» обязаны быть одним списком, иначе повторная планировка
затрёт previous_state строки, отправку которой пропускает apply_actions.

Транспорт (client.py) намеренно НЕ поднимает исключение на ошибку уровня
ЭЛЕМЕНТА (result.AddResults[]/SetResults[].Errors, например код 8800
«кампания не найдена») — она приходит в успешном HTTP-ответе и остаётся в
result для разбора здесь. Без разбора такая ошибка выглядела бы как success:
статус ушёл бы в 'applied', а на следующем прогоне идемпотентный ключ нашёлся
бы уже применённым и действие, которое физически не создалось в кабинете,
навсегда осталось бы недостижимым для повторной попытки.
"""

from typing import Any, Dict, List, Optional, Tuple

from sync.agent.writer.client import is_outcome_unknown
from sync.agent.writer.db import FINAL_STATUSES
from sync.agent.writer.units import delta_to_api

# key корректировки → форма API. Проверено probe (задача 1).
_DEMOGRAPHIC_KEYS = {"GENDER_MALE", "GENDER_FEMALE"}

# Тип корректировки устройства → имя поля в теле запроса bidmodifiers.add.
# У Директа это ТРИ разных типа, а не один «мобильный»: коэффициент,
# посчитанный для десктопа, отправленный как MobileAdjustment, изменил бы
# ставку не тому сегменту (образец разбора — sync/edu_direct_settings.py:844-866).
_DEVICE_ADJUSTMENT_FIELD = {
    "MOBILE_ADJUSTMENT": "MobileAdjustment",
    "DESKTOP_ADJUSTMENT": "DesktopAdjustment",
    "TABLET_ADJUSTMENT": "TabletAdjustment",
}

# API-метод → имя коллекции результатов по элементам в ответе.
_RESULT_COLLECTION = {"add": "AddResults", "set": "SetResults"}


def to_api_call(action: Dict[str, Any]) -> Tuple[str, str, Dict[str, Any]]:
    """Действие → (сервис, метод, параметры).

    Здесь и только здесь дельта из плана превращается в 100-базный
    коэффициент Директа (units.delta_to_api): payload несёт человеческие
    единицы (30 = «+30 %»), наружу уходит 130.
    """
    kind = str(action.get("action_kind") or "")
    payload = action.get("payload") or {}

    if kind == "bidmodifier.set":
        return "bidmodifiers", "set", {
            "BidModifiers": [{"Id": payload["Id"],
                              "BidModifier": delta_to_api(payload["BidModifier"])}]
        }

    if kind == "bidmodifier.add":
        item: Dict[str, Any] = {"CampaignId": int(payload["CampaignId"])}
        coefficient = delta_to_api(payload["BidModifier"])
        direct_type = payload.get("Type")
        key = str(payload.get("key") or "")

        device_field = _DEVICE_ADJUSTMENT_FIELD.get(str(direct_type))
        if device_field:
            item[device_field] = {"BidModifier": coefficient}
        elif direct_type == "DEMOGRAPHICS_ADJUSTMENT":
            adjustment: Dict[str, Any] = {"BidModifier": coefficient}
            if key in _DEMOGRAPHIC_KEYS:
                adjustment["Gender"] = key
            else:
                adjustment["Age"] = key
            item["DemographicsAdjustments"] = [adjustment]
        elif direct_type == "REGIONAL_ADJUSTMENT":
            item["RegionalAdjustments"] = [{"RegionId": int(key),
                                            "BidModifier": coefficient}]
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


def apply_actions(client, actions: List[Dict[str, Any]], db_module,
                  lease=None) -> Dict[str, Any]:
    """Применяет действия по одному: журнал → отправка → отметка результата.

    lease — аренда прогона (db.RunLease). Перед КАЖДЫМ изменяющим запросом
    проверяется, что она всё ещё наша: аренда берётся на час, а прогон по
    сотням кампаний живёт дольше, и потерянная на ходу аренда означает, что
    второй прогон уже стартовал и пишет в тот же кабинет. Потеря аренды
    поднимает db.RunLeaseLost и обрывает применение — это не ошибка одного
    действия, а условие, при котором писать нельзя вообще. None — вызывающий
    код без аренды (тесты, разовый разбор).

    Статусов в машине журнала семь. Пять из них ставит эта функция после
    отправки:
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
      - 'failed'   — запрос ТОЧНО не ушёл (ошибка до отправки, соединение не
                     установлено, отказ уровня запроса от самого Директа)
                     ИЛИ ответ пришёл, но разобрать его нечем (_element_errors
                     не знает метод — новый вид операции без записи в
                     _RESULT_COLLECTION). Переприменяется на следующем прогоне;
      - 'stale'    — исход НЕИЗВЕСТЕН: таймаут на записи, разрыв после
                     отправки тела, недоступность сервиса после ретраев
                     (client.is_outcome_unknown). Изменение может быть живым в
                     кабинете, поэтому строка уходит в тот же контур, что и
                     обрыв процесса: под наблюдение сторожа и под риск-бюджет,
                     без повторной отправки. Считать такое 'failed' — дыра
                     того же класса и размера, что закрывал механизм зависших
                     строк: изменение исчезает из виду навсегда.

    Ещё два ставятся вне этой функции, и знать про них здесь обязательно —
    именно из-за них действие может быть пропущено или перехвачено:
      - 'planned'     — db.insert_action, до отправки. Строка уже описывает
                        прошлое состояние, но запроса ещё не было;
      - 'rolled_back' — сторож красных линий (agent_e1_watchdog), после
                        успешного возврата.

    Отметка результата может не лечь: между проверкой статуса и отметкой
    строку мог забрать сторож (перевод в 'stale' и откат). Тогда действие
    попадает в счётчик 'conflicted' — журнал уже описывает исход точнее, чем
    эта отправка, и затирать его нельзя (db.MARK_ACTION_SQL держит гард).
    """
    applied = skipped = failed = rejected = dry_run = unknown = conflicted = 0
    details: List[Dict[str, Any]] = []

    def _mark(action_id: str, status: str, response: Dict[str, Any]) -> bool:
        """True — отметка легла. False — строку забрал другой контур.

        Модуль журнала мог не обновиться (тесты со своим двойником, старая
        сигнатура) — None трактуется как «легла», чтобы отсутствие возврата
        не превращалось в поток ложных конфликтов.
        """
        return db_module.mark_action(action_id, status, response) is not False

    for action in actions:
        existing = db_module.find_action_by_key(action["idempotency_key"])
        if existing and existing.get("status") in set(FINAL_STATUSES):
            skipped += 1
            details.append({"key": action["idempotency_key"], "result": "skipped"})
            continue

        # Владение арендой перепроверяется ДО записи в журнал и до отправки:
        # если аренда потеряна, параллельный прогон уже мог начать писать в
        # тот же кабинет, и добавлять к этому свой запрос нельзя. Исключение
        # намеренно не ловится ниже по стеку — прогон обязан остановиться.
        if lease is not None:
            lease.guard()

        action_id = db_module.insert_action(action)
        try:
            service, method, params = to_api_call(action)
            response = client.mutate(service, method, params)
            if response.get("dry_run"):
                status = "dry_run"
            else:
                errors = _element_errors(method, response)
                status = "rejected" if errors else "applied"
            if not _mark(action_id, status, response):
                conflicted += 1
                details.append({"key": action["idempotency_key"], "result": "conflicted",
                                "attempted_status": status})
                continue
            applied += 1 if status == "applied" else 0
            rejected += 1 if status == "rejected" else 0
            dry_run += 1 if status == "dry_run" else 0
            details.append({"key": action["idempotency_key"], "result": status})
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"[:400]
            if is_outcome_unknown(exc):
                landed = db_module.mark_unknown_outcome(action_id, reason)
                if landed is False:
                    conflicted += 1
                    details.append({"key": action["idempotency_key"],
                                    "result": "conflicted",
                                    "attempted_status": "stale"})
                    continue
                unknown += 1
                details.append({"key": action["idempotency_key"],
                                "result": "unknown_outcome", "error": str(exc)[:200]})
                continue
            if not _mark(action_id, "failed", {"error": reason}):
                conflicted += 1
                details.append({"key": action["idempotency_key"], "result": "conflicted",
                                "attempted_status": "failed"})
                continue
            failed += 1
            details.append({"key": action["idempotency_key"], "result": "failed",
                            "error": str(exc)[:200]})

    # dry_run — полноправный счётчик, а не «ничего не произошло»: в режиме
    # репетиции ВСЕ действия получают этот статус, и отчёт без него показывал
    # ровные нули по всем счётчикам. Ровно по этому отчёту принимается решение
    # включать боевую запись, и он обязан показывать объём репетиции.
    # unknown_outcome — отдельный счётчик, а не часть failed: это ЖИВЫЕ
    # непроверенные изменения, они заняли риск-бюджет и стоят под наблюдением.
    # conflicted — строки, исход которых записал другой контур.
    return {"applied": applied, "skipped": skipped, "failed": failed, "rejected": rejected,
            "dry_run": dry_run, "unknown_outcome": unknown, "conflicted": conflicted,
            "details": details}
