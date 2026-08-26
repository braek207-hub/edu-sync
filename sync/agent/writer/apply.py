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

from sync.agent import rejects
from sync.agent.writer.client import (BATCH_LIMIT, is_outcome_unknown,
                                      is_transient_quota)
from sync.agent.writer.db import FINAL_STATUSES

# Доля суточного лимита баллов, ниже которой запись не начинается: остаток
# нужен ретраям чтения, сторожу отката и завтрашним синкам — выжигать его в
# ноль мутациями нельзя.
UNITS_RESERVE_SHARE = 0.05
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
                      "update": "UpdateResults", "suspend": "SuspendResults",
                      "resume": "ResumeResults"}

# (сервис, метод) → имя коллекции в ТЕЛЕ запроса, элементы которой можно
# сложить в один запрос. Перечень закрытый и по форме тела, а не по сервису:
# у campaigns.suspend тело — SelectionCriteria с массивом Id, то есть другая
# форма, и класть её в общий механизм значит склеивать разные семантики.
# Такое действие едет одно, как и раньше.
_BATCH_COLLECTION = {("bidmodifiers", "set"): "BidModifiers",
                     ("bidmodifiers", "add"): "BidModifiers",
                     ("campaigns", "update"): "Campaigns"}


def _batch_collection(service: str, method: str,
                      params: Dict[str, Any]) -> Optional[str]:
    """Имя коллекции, если это тело можно объединить с соседями. Иначе None."""
    collection = _BATCH_COLLECTION.get((service, method))
    if not collection:
        return None
    items = params.get(collection)
    # to_api_call кладёт РОВНО один элемент. Тело с другим числом элементов
    # собрал не он, и объединять его вслепую нельзя.
    if not isinstance(items, list) or len(items) != 1:
        return None
    return collection


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

    if kind == "tcpa.set":
        # Цель CPA живёт внутри BiddingStrategy, и блок собран планом
        # (writer/tcpa.py) из ПРОЧИТАННОГО состояния с заменой одного лишь
        # AverageCpa: структура в API заменяется целиком.
        return "campaigns", "update", {
            "Campaigns": [{"Id": int(payload["CampaignId"]),
                           "TextCampaign": {
                               "BiddingStrategy": payload["BiddingStrategy"]}}]
        }

    if kind == "placement.exclude":
        # Список запрещённых площадок в API заменяется целиком; объединение
        # прежних и новых собрал план (writer/placements.py).
        return "campaigns", "update", {
            "Campaigns": [{"Id": int(payload["CampaignId"]),
                           "ExcludedSites": payload["ExcludedSites"]}]
        }

    if kind == "negative.add":
        # Список минус-фраз в API заменяется ЦЕЛИКОМ, и план (writer/negatives)
        # уже собрал объединение прежних и новых: здесь только упаковка.
        return "campaigns", "update", {
            "Campaigns": [{"Id": int(payload["CampaignId"]),
                           "NegativeKeywords": payload["NegativeKeywords"]}]
        }

    if kind == "budget.set_daily":
        return "campaigns", "update", {
            "Campaigns": [{"Id": int(payload["CampaignId"]),
                           "DailyBudget": payload["DailyBudget"]}]
        }

    if kind == "campaign.suspend":
        # Пауза — отдельный метод API с SelectionCriteria, а не поле update:
        # состоянием показов Директ управляет только так.
        return "campaigns", "suspend", {
            "SelectionCriteria": {"Ids": [int(payload["CampaignId"])]}
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


def _element_errors(method: str, response: Dict[str, Any],
                    index: int = 0) -> Optional[List[Dict[str, Any]]]:
    """Ошибки уровня элемента в успешном HTTP-ответе (result.*Results[].Errors).

    index — место элемента в отправленном теле. Другого ключа у ответа нет:
    Директ отвечает позиционно, result.*Results[i] описывает i-й отправленный
    элемент. Поэтому нарезка батчей обязана сохранять порядок
    (client.split_batches), а разбор — спрашивать свой индекс, а не первый
    попавшийся результат.

    Коллекция пуста (известный метод, но результатов нет) — считать нечего,
    возвращаем None, а не пустой список, чтобы вызывающий код не путал «нет
    данных для разбора» с «разобрали, ошибок нет».

    А вот ответ КОРОЧЕ отправленного — не то же самое, что пустой: часть
    элементов Директ не описал вовсе, и чем они закончились, неизвестно.
    Пометить их applied значило бы соврать журналу о живых изменениях,
    поэтому запрашивание индекса за пределами ответа падает — и весь батч
    уходит в 'stale', под сторожа и риск-бюджет.

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
    if not items and index == 0:
        return None
    if index >= len(items):
        raise ValueError(
            f"ответ {method} описывает {len(items)} элементов, а спрашивают "
            f"{index + 1}-й: чем закончились неописанные — неизвестно")
    errors = (items[index] or {}).get("Errors")
    return errors or None


def _element_response(method: str, response: Dict[str, Any], index: int,
                      size: int) -> Dict[str, Any]:
    """Часть ответа, относящаяся к ЭТОМУ элементу батча.

    Класть в каждую строку журнала общий ответ нельзя: откат добавленной
    корректировки ищет её Id в response.AddResults[0].Id
    (writer/rollback._added_modifier_id). При общем ответе все десять строк
    батча вернули бы Id ПЕРВОГО элемента — сторож откатывал бы чужую
    корректировку, а свою оставлял жить.

    Одиночный запрос отдаёт ответ целиком, как и раньше: у него один элемент,
    и резать нечего.
    """
    if size <= 1:
        return response
    collection_key = _RESULT_COLLECTION.get(method)
    items = (response.get(collection_key) or []) if collection_key else []
    element = items[index] if index < len(items) else {}
    return {collection_key: [element], "batch": {"size": size, "index": index}}


def _rehearsal_response(response: Dict[str, Any], collection: Optional[str],
                        item: Optional[Dict[str, Any]], index: int,
                        size: int) -> Dict[str, Any]:
    """Пометка репетиции для ОДНОЙ строки батча.

    В репетиции транспорт возвращает одну пометку на весь батч. Строка
    журнала описывает одно действие, и тело в ней должно быть тоже одно —
    иначе отчёт репетиции (по нему принимается решение включать боевую
    запись) показывал бы десять одинаковых строк с десятью телами каждая.
    """
    if size <= 1 or not collection:
        return response
    return {**response, "params": {collection: [item]},
            "batch": {"size": size, "index": index}}


def apply_actions(client, actions: List[Dict[str, Any]], db_module,
                  lease=None, stage: str = "e1") -> Dict[str, Any]:
    """Применяет действия по одному: журнал → отправка → отметка результата.

    stage — код такта для строк отказов. Такт здесь один (Э1), но подписывать
    строку журнала отказов константой внутри применения нельзя: у отказа,
    прочитанного из базы, стадия — единственное, что отвечает на вопрос «кто
    это не смог», и вписывать её должен тот, кто знает ответ.

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
    deferred = units_low = 0
    details: List[Dict[str, Any]] = []
    # Отказы строками, а не только счётчиком: причина, объект и цена уезжают в
    # чёрный ящик, где на истории видно, упирается ли такт в баллы каждый день
    # или это была разовая просадка кабинета.
    reject_rows: List[Dict[str, Any]] = []

    def _mark(action_id: str, status: str, response: Dict[str, Any]) -> bool:
        """True — отметка легла. False — строку забрал другой контур.

        Модуль журнала мог не обновиться (тесты со своим двойником, старая
        сигнатура) — None трактуется как «легла», чтобы отсутствие возврата
        не превращалось в поток ложных конфликтов.
        """
        return db_module.mark_action(action_id, status, response) is not False

    def _record(action: Dict[str, Any], action_id: str, status: str,
                response: Dict[str, Any]) -> None:
        """Исход одного действия: отметка в журнале + счётчик отчёта."""
        nonlocal applied, rejected, dry_run, conflicted
        if not _mark(action_id, status, response):
            conflicted += 1
            details.append({"key": action["idempotency_key"], "result": "conflicted",
                            "attempted_status": status})
            return
        applied += 1 if status == "applied" else 0
        rejected += 1 if status == "rejected" else 0
        dry_run += 1 if status == "dry_run" else 0
        details.append({"key": action["idempotency_key"], "result": status})

    def _record_failure(action: Dict[str, Any], action_id: str,
                        exc: Exception, sent: bool) -> None:
        """Исход действия, которому не повезло. Граница — по факту отправки.

        sent=True означает, что запрос УЖЕ состоялся, а сломалось то, что
        было после: разбор ответа, нарезка батча. Тогда исход неизвестен по
        определению, и 'failed' здесь означал бы переприменение уже
        совершённого изменения (для bidmodifiers.add — второй объект в
        кабинете).
        """
        nonlocal failed, deferred, unknown, conflicted
        reason = f"{type(exc).__name__}: {exc}"[:400]
        if sent:
            reason = f"ответ получен, но не разобран — {reason}"
        if sent or is_outcome_unknown(exc):
            landed = db_module.mark_unknown_outcome(action_id, reason)
            if landed is False:
                conflicted += 1
                details.append({"key": action["idempotency_key"],
                                "result": "conflicted",
                                "attempted_status": "stale"})
                return
            unknown += 1
            details.append({"key": action["idempotency_key"],
                            "result": "unknown_outcome", "error": str(exc)[:200]})
            return
        if is_transient_quota(exc):
            # Квота/временная недоступность: Директ отклонил вызов, не
            # применив его. Не 'failed' — попытка не жжётся (кабинет виноват,
            # не действие); следующий прогон переприменит.
            if not _mark(action_id, "deferred", {"error": reason}):
                conflicted += 1
                details.append({"key": action["idempotency_key"],
                                "result": "conflicted",
                                "attempted_status": "deferred"})
                return
            deferred += 1
            details.append({"key": action["idempotency_key"],
                            "result": "deferred", "error": str(exc)[:200]})
            return
        if not _mark(action_id, "failed", {"error": reason}):
            conflicted += 1
            details.append({"key": action["idempotency_key"], "result": "conflicted",
                            "attempted_status": "failed"})
            return
        failed += 1
        details.append({"key": action["idempotency_key"], "result": "failed",
                        "error": str(exc)[:200]})

    def _journal_row(action: Dict[str, Any]) -> str:
        """Строка журнала с прошлым состоянием — до всякой отправки.

        Владение арендой перепроверяется ровно здесь: если аренда потеряна,
        параллельный прогон уже мог начать писать в тот же кабинет, и
        добавлять к этому свою строку нельзя. Проверка стоит перед записью,
        а не в начале круга, чтобы действие, отложенное в следующий батч, не
        спрашивало аренду по разу на круг. Исключение намеренно не ловится
        ниже по стеку — прогон обязан остановиться.
        """
        if lease is not None:
            lease.guard()
        return db_module.insert_action(action)

    def _mark_batch_sent(batch: List[Dict[str, Any]]) -> None:
        """«Запрос отправляется» — в журнал НЕПОСРЕДСТВЕННО перед mutate.

        Отметка стоит именно здесь, а не при заведении строки: между
        заведением и отправкой теперь помещается сборка батча, и она может
        закончиться ничем (потеряна аренда, упало тело соседнего действия).
        Строка с sent_at, чей запрос так и не ушёл, сторож закрыл бы как
        зависшую с неизвестным исходом — то есть как живое изменение, которого
        в кабинете нет: она заняла бы риск-бюджет и попала под откат. Без
        sent_at та же строка закрывается как никогда-не-отправленная
        ('aborted', db.mark_stale_planned) и переприменяется следующим
        прогоном.

        Репетиция не отмечается: dry-run никуда не ходит, и sent_at лгал бы.
        """
        if not client.is_write_allowed():
            return
        for item in batch:
            db_module.mark_sent(item["action_id"])

    # Очередь такта. «Проверено» помнится по действию, а не проверяется заново
    # каждый круг: действие, отложенное в следующий батч (занят объект, другой
    # вид), иначе спрашивало бы журнал по разу на круг — при трёхстах действиях
    # это тысячи лишних запросов к базе за прогон.
    queue: List[Dict[str, Any]] = [{"action": a, "checked": False} for a in actions]

    while queue:
        rest: List[Dict[str, Any]] = []
        batch: List[Dict[str, Any]] = []
        service = method = collection = None

        for entry in queue:
            action = entry["action"]
            # Батч закрыт: место кончилось или в нём уже лежит несбатчиваемое
            # тело (оно едет одно). Остальное — следующим кругом, БЕЗ похода
            # в журнал: предпроверки для этих действий ещё не делались.
            if batch and (len(batch) >= BATCH_LIMIT or collection is None):
                rest.append(entry)
                continue

            # Порог баллов — на КАЖДОМ круге, а не один раз за действие:
            # остаток обновляется каждым ответом API, и действие, отложенное
            # в третий батч, может уже не иметь на что уехать. Порог — доля
            # от суточного ЛИМИТА из заголовка Units этого же кабинета, не
            # магическое число: лимиты кабинетов различаются на порядок.
            left, limit = (getattr(client, "units_left", None),
                           getattr(client, "units_limit", None))
            if left is not None and limit and left < UNITS_RESERVE_SHARE * limit:
                units_low += 1
                details.append({"key": action["idempotency_key"],
                                "result": "units_low"})
                # Цена берётся из самого действия: к этому моменту риск-бюджет
                # её уже назвал (agent_e1 кладёт risk_rub в строку перед
                # отправкой), и брать её больше неоткуда — прогон о
                # пропущенном хвосте не знает.
                reject_rows.append(rejects.row(
                    action, rejects.UNITS_LOW,
                    account=str(action.get("account") or ""), stage=stage,
                    risk_rub=float(action.get("risk_rub") or 0.0)))
                continue

            if not entry["checked"]:
                existing = db_module.find_action_by_key(action["idempotency_key"])
                if existing and existing.get("status") in set(FINAL_STATUSES):
                    skipped += 1
                    details.append({"key": action["idempotency_key"],
                                    "result": "skipped"})
                    continue
                entry["checked"] = True

            try:
                call = to_api_call(action)
            except Exception as exc:
                # Тело собрать не удалось (неизвестный демографический ключ,
                # регион названием). Запрос не ушёл и уйти не мог, поэтому
                # действие закрывается сразу и батчу не мешает.
                _record_failure(action, _journal_row(action), exc, sent=False)
                continue

            a_service, a_method, a_params = call
            a_collection = _batch_collection(a_service, a_method, a_params)
            object_id = str(action.get("object_id") or "")
            # Два элемента про ОДИН объект в одном теле — не то же самое, что
            # два запроса подряд: у campaigns.update второй элемент затирает
            # первый, а у корректировок такое соседство нигде не проверено.
            # Батч меняет число запросов, а не соседство элементов.
            if batch and ((a_service, a_method) != (service, method)
                          or a_collection is None
                          or object_id in {e["object_id"] for e in batch}):
                rest.append(entry)
                continue

            service, method, collection = a_service, a_method, a_collection
            batch.append({
                "action": action,
                "action_id": _journal_row(action),
                "object_id": object_id,
                "params": a_params,
                "item": a_params[a_collection][0] if a_collection else None,
            })

        queue = rest
        if not batch:
            # Круг, не отправивший НИЧЕГО при непустом остатке, — заклинивший
            # цикл: прогон крутился бы до конца аренды и мимо своего окна, а
            # сверка читала бы кабинет посреди записи. По построению так быть
            # не может (в остаток действие попадает только при непустом
            # батче), но зависание — худший из возможных исходов, и падение
            # здесь честнее вечного круга.
            if queue:
                raise RuntimeError(
                    f"сборка батчей не продвигается: {len(queue)} действий в "
                    f"остатке, отправлено ноль")
            continue

        # Факт отправки отмечается СРАЗУ после возврата из транспорта, до
        # любого разбора ответа. Всё, что падает после этой отметки, —
        # неизвестный исход, а не «запрос не ушёл»: запрос уже состоялся, и
        # для bidmodifiers.add переприменение означало бы ВТОРОЙ объект в
        # кабинете.
        sent = False
        try:
            _mark_batch_sent(batch)
            if collection:
                response = client.mutate_batch(service, method, collection,
                                               [e["item"] for e in batch])
            else:
                response = client.mutate(service, method, batch[0]["params"])
            # Репетиция отправкой не считается: mutate вернул пометку, не
            # сходив в сеть (client.WriteClient.mutate). Пометь мы её как
            # отправку — исключение при разборе увело бы строку репетиции в
            # 'stale', то есть в живые непроверенные изменения боевого
            # журнала, за которыми ничего не стоит.
            rehearsal = bool(isinstance(response, dict) and response.get("dry_run"))
            sent = not rehearsal
            if rehearsal:
                for index, item in enumerate(batch):
                    _record(item["action"], item["action_id"], "dry_run",
                            _rehearsal_response(response, collection, item["item"],
                                                index, len(batch)))
                continue
            # Ответ разбирается ЦЕЛИКОМ прежде, чем лечь хоть одной отметкой:
            # ошибка разбора на пятом элементе не должна оставлять четыре
            # строки применёнными, а остальные — в неизвестности. Либо весь
            # батч разобран, либо весь батч неизвестен.
            statuses = ["rejected" if _element_errors(method, response, index) else "applied"
                        for index in range(len(batch))]
            for index, item in enumerate(batch):
                _record(item["action"], item["action_id"], statuses[index],
                        _element_response(method, response, index, len(batch)))
        except Exception as exc:
            # Ошибка уровня ЗАПРОСА — общая на весь батч: не применилось
            # НИЧЕГО, и каждый элемент обязан получить свой исход отдельной
            # строкой. Ошибки уровня ЭЛЕМЕНТА сюда не попадают (они приезжают
            # в result, см. client._call) — иначе один плохой элемент отменял
            # бы применённых соседей.
            for item in batch:
                _record_failure(item["action"], item["action_id"], exc, sent)

    # dry_run — полноправный счётчик, а не «ничего не произошло»: в режиме
    # репетиции ВСЕ действия получают этот статус, и отчёт без него показывал
    # ровные нули по всем счётчикам. Ровно по этому отчёту принимается решение
    # включать боевую запись, и он обязан показывать объём репетиции.
    # unknown_outcome — отдельный счётчик, а не часть failed: это ЖИВЫЕ
    # непроверенные изменения, они заняли риск-бюджет и стоят под наблюдением.
    # conflicted — строки, исход которых записал другой контур.
    return {"applied": applied, "skipped": skipped, "failed": failed, "rejected": rejected,
            "dry_run": dry_run, "unknown_outcome": unknown, "conflicted": conflicted,
            "deferred": deferred, "units_low": units_low,
            # Строки отказов — отдельным полем от счётчиков, тем же порядком,
            # что и в agent_e1: счётчики читает человек в логе, строки едут в
            # edu_agent_rejects. Пустой список здесь — не «поле забыли», а
            # «отказов не было», и вызывающий код обязан видеть разницу.
            "rejects": reject_rows,
            "details": details}
