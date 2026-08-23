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

Инвариант «песочный клиент журнала не касается» держится здесь кодом:
apply_actions отказывается стартовать (SandboxApplyRefusal), если клиент
собран с sandbox=True и dry_run=False, — до первой записи в журнал. Это
второй рубеж; первый — отказ main() ДО обращения к БД (agent_e1.refusal).
"""

from typing import Any, Dict, List, Optional, Tuple

from sync.agent.writer.client import is_outcome_unknown
from sync.agent.writer.db import FINAL_STATUSES
from sync.agent.writer.plan import DEMOGRAPHIC_FIELD
from sync.agent.writer.units import delta_to_api

# Второй рубеж инварианта «песочный клиент журнала не касается» (первый —
# отказ ДО первого обращения к БД в agent_e1.refusal/agent_e1_watchdog.refusal).
# Тот рубеж защищает штатный запуск через main(); этот — саму функцию
# применения, которую вызывающий код (живой тест, разовый скрипт) может
# собрать и вызвать напрямую, минуя main(). «Песочница + запись» здесь та же
# запрещённая комбинация, что и там: боевые логины в песочнице недоступны,
# а запись о попытке всё равно ушла бы в БОЕВОЙ журнал (база одна) —
# при транспортной ошибке такая строка получает статус «исход неизвестен»,
# занимает риск-бюджет и подставляет сторожа под откат объекта, которого в
# боевом кабинете никогда не было.
#
# Атрибуты читаются через getattr с умолчаниями, повторяющими умолчания
# самого WriteClient (sandbox=True, dry_run=True): боевой путь клиент
# всегда создаёт с явными sandbox/dry_run, так что для него поведение не
# меняется ни на шаг; это только подстраховка для кода, который передал
# объект без этих атрибутов вовсе.
SANDBOX_APPLY_REFUSAL = (
    "запрещённая комбинация «песочница + запись»: клиент собран с "
    "sandbox=True и без dry_run — применение отказывается писать в "
    "БОЕВОЙ журнал от имени песочницы. Нужна боевая запись — "
    "WriteClient(sandbox=False, dry_run=False); нужна репетиция — "
    "dry_run=True"
)


class SandboxApplyRefusal(RuntimeError):
    """Применение отказалось начинать: клиент — «песочница + запись».

    Держит инвариант «песочный клиент журнала не касается» кодом, а не
    дисциплиной вызывающего: main() уже отказывается стартовать в этой
    комбинации (agent_e1.refusal), но apply_actions — не единственная точка
    входа в применение, и второй рубеж здесь дешевле любой будущей утечки
    инварианта через новый вызывающий код.
    """


def _sandbox_apply_refused(client) -> bool:
    sandbox = bool(getattr(client, "sandbox", True))
    dry_run = bool(getattr(client, "dry_run", True))
    return sandbox and not dry_run

# Ключ демографического сегмента → поле тела запроса (Gender или Age).
# Справочник один на планирование и на сборку запроса (plan.DEMOGRAPHIC_FIELD),
# потому что расхождение между «какие ключи мы разрешаем» и «как мы их
# раскладываем по полям» и есть тот класс ошибки, ради которого справочник
# заводится.
#
# Прежде здесь стояло «если ключ не пол, значит возраст»: любое значение,
# прошедшее планирование, молча становилось Age и уезжало в API. «Не
# определено» (UNKNOWN) из отчётов Директа проходило этот путь целиком и
# возвращалось отказом уровня элемента.
_DEMOGRAPHIC_FIELD = DEMOGRAPHIC_FIELD

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
# У campaigns.update своя коллекция (UpdateResults) — не «SetResults», как у
# bidmodifiers.set. Ошибись здесь, и отказ уровня элемента прочитался бы как
# успех: список результатов просто не нашёлся бы, а пустой список ошибок
# означает «принято».
_RESULT_COLLECTION = {"add": "AddResults", "set": "SetResults",
                      "update": "UpdateResults"}


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
            field = _DEMOGRAPHIC_FIELD.get(key)
            if field is None:
                raise ValueError(
                    f"демографический ключ вне перечня API Директа: {key!r} "
                    f"(допустимы {', '.join(sorted(_DEMOGRAPHIC_FIELD))})")
            item["DemographicsAdjustments"] = [{"BidModifier": coefficient,
                                                field: key}]
        elif direct_type == "REGIONAL_ADJUSTMENT":
            item["RegionalAdjustments"] = [{"RegionId": int(key),
                                            "BidModifier": coefficient}]
        else:
            raise ValueError(f"неизвестный тип корректировки: {direct_type}")
        return "bidmodifiers", "add", {"BidModifiers": [item]}

    if kind == "budget.set":
        # Блок BiddingStrategy собран планом (writer/budget.py) из
        # ПРОЧИТАННОГО состояния с заменой одного лишь WeeklySpendLimit:
        # структура в API заменяется целиком, и пересборка потеряла бы
        # соседние поля стратегии.
        return "campaigns", "update", {
            "Campaigns": [{"Id": int(payload["CampaignId"]),
                           "TextCampaign": {
                               "BiddingStrategy": payload["BiddingStrategy"]}}]
        }

    if kind == "budget.set_daily":
        return "campaigns", "update", {
            "Campaigns": [{"Id": int(payload["CampaignId"]),
                           "DailyBudget": payload["DailyBudget"]}]
        }

    if kind == "schedule.set":
        # Расписание применяется ЦЕЛИКОМ и через саму кампанию: у Директа нет
        # способа поменять один час. Тело собрано планом (writer/schedule.py),
        # включая перенос соседних полей блока — праздничного режима и учёта
        # рабочих выходных, которые настроены человеком.
        return "campaigns", "update", {
            "Campaigns": [{"Id": int(payload["CampaignId"]),
                           "TimeTargeting": payload["TimeTargeting"]}]
        }

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
    обязан упасть явно, а не молча вернуть «ошибок нет». Падение происходит
    ПОСЛЕ отправки, поэтому apply_actions трактует его как неизвестный исход
    ('stale'), а не как неудачу.
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
      - 'failed'   — запрос ТОЧНО не ушёл: ошибка ДО отправки (сборка тела,
                     неизвестный демографический ключ), соединение не
                     установлено, отказ уровня запроса от самого Директа.
                     Переприменяется на следующем прогоне;
      - 'stale'    — исход НЕИЗВЕСТЕН: таймаут на записи, разрыв после
                     отправки тела, недоступность сервиса после ретраев
                     (client.is_outcome_unknown) — И ЛЮБОЕ исключение ПОСЛЕ
                     возврата из транспорта, например ответ, который нечем
                     разобрать (_element_errors не знает метод — новый вид
                     операции без записи в _RESULT_COLLECTION). Граница
                     проходит по факту отправки, а не по типу исключения:
                     после mutate запрос уже состоялся, и переприменение
                     bidmodifier.add создало бы в кабинете ВТОРОЙ объект.
                     Изменение может быть живым, поэтому строка уходит в тот
                     же контур, что и обрыв процесса: под наблюдение сторожа
                     и под риск-бюджет, без повторной отправки. Считать такое
                     'failed' — дыра того же класса и размера, что закрывал
                     механизм зависших строк: изменение исчезает из виду
                     навсегда.

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
    # Отказ ДО первого обращения к БД — тот же порядок, что и в main(): ничего
    # не должно лечь в журнал прежде, чем комбинация будет проверена.
    if _sandbox_apply_refused(client):
        raise SandboxApplyRefusal(SANDBOX_APPLY_REFUSAL)

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
        # Факт отправки отмечается СРАЗУ после возврата из транспорта, до
        # любого разбора ответа. Всё, что падает после этой отметки, —
        # неизвестный исход, а не «запрос не ушёл»: запрос уже состоялся, и
        # для bidmodifiers.add переприменение означало бы ВТОРОЙ объект в
        # кабинете. Прежде обе половины докстринга статуса 'failed' («точно
        # не ушло» И «разобрать нечем») лечились одним лекарством, и выбрано
        # было небезопасное.
        sent = False
        try:
            service, method, params = to_api_call(action)
            response = client.mutate(service, method, params)
            # Репетиция отправкой не считается: mutate вернул пометку, не
            # сходив в сеть (client.WriteClient.mutate). Пометь мы её как
            # отправку — исключение при разборе увело бы строку репетиции в
            # 'stale', то есть в живые непроверенные изменения боевого
            # журнала, за которыми ничего не стоит.
            rehearsal = bool(isinstance(response, dict) and response.get("dry_run"))
            sent = not rehearsal
            if rehearsal:
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
            if sent:
                # Запрос ушёл и вернулся — а дальше сломался разбор. Исход
                # неизвестен по определению: 'failed' здесь означал бы
                # переприменение уже совершённого изменения.
                reason = f"ответ получен, но не разобран — {reason}"
            if sent or is_outcome_unknown(exc):
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
