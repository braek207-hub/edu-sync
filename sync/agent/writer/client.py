# -*- coding: utf-8 -*-
"""
sync/agent/writer/client.py — транспорт записи в Яндекс Директ.

Формы вызовов взяты с рабочего d:\\vscode\\EDU кампании\\direct\\client.py: тот
репозиторий недоступен из CI, поэтому код здесь самодостаточный, но повторяет
проверенные на проде решения (ретраи на 5xx, учёт Units). Батчи появились
вместе с кодом, который их использует (apply.py: отбор полосами даёт сотни
действий за прогон, и запрос за элементом в окно между кроном Э1 и сверкой
уже не помещается). Стиль запроса согласован с sync/agent/segments.py
(_api_headers/_api_post — тот же кабинет, та же кодировка ответа).

Два предохранителя по умолчанию:
  - sandbox=True — боевой кабинет требует явного решения;
  - dry_run=True — мутация не уходит без явного --apply.

Агент никогда не удаляет объекты Директа — это не транспортное ограничение
(delete как метод API технически проходит через mutate), а решение уровня
вызывающего кода: red-line guard не пускает action_kind='delete' до транспорта.

Токен резолвится лениво (при первом сетевом вызове, не в __init__): песочница
и dry-run проверяются в тестах без сети и без DIRECT_TOKEN в окружении.
"""

import json
import os
import time
from typing import Any, Dict, List, Optional

import requests

PROD_BASE = "https://api.direct.yandex.com/json/v5"
SANDBOX_BASE = "https://api-sandbox.direct.yandex.com/json/v5"

RETRY_CODES = {500, 502, 503, 504}

# Сколько раз отправлять одно и то же тело. Число РАЗНОЕ для чтения и записи,
# и это не настройка, а граница безопасности.
#
# Чтение идемпотентно: повтор campaigns.get при недоступности сервиса даёт тот
# же ответ и ничего не меняет в кабинете — повторять полезно.
#
# У записи идемпотентности нет: у bidmodifiers.add нет ключа на стороне
# Директа, и пятисотая может прийти уже ПОСЛЕ применения (балансировщик,
# таймаут на стороне сервиса, сбой на ответе — см. разбор ниже по коду).
# Повтор в этом случае создаёт в кабинете ВТОРОЙ объект: одна сетевая икота —
# до четырёх корректировок вместо одной, и Id первых трёх не знает никто.
# Строки журнала у них нет, красной линии нет, откат невозможен. Это ровно тот
# сценарий, ради которого заведена аренда прогона (db.run_lock), только внутри
# одного прогона — и защищаться от него надо так же жёстко.
#
# Поэтому изменяющий запрос отправляется РОВНО ОДИН раз, а недоступность
# сервиса честно возвращается как «исход неизвестен» (outcome_unknown=True):
# механизм для такого исхода уже есть — строка уходит в тот же контур, что и
# обрыв процесса после отправки ('stale'), под наблюдение сторожа и под
# риск-бюджет, без повторной отправки.
READ_RETRIES = 4
WRITE_ATTEMPTS = 1

# Сколько элементов едет в ОДНОМ изменяющем запросе.
#
# Число намеренно маленькое. У сервисов API v5 лимиты на объём запроса
# разные, и ни один из них здесь не проверен живым прогоном: у агента нет
# доступа к боевому кабинету из тестов. Единственный размер пачки,
# подтверждённый на проде в этом репозитории, — CAMPAIGN_CHUNK = 10
# (sync/agent/segments.py, чтение кампаний), и десятка заведомо не больше
# лимита любого сервиса записи. Больше — гадание, а перебор Директ отвергает
# ЗАПРОС ЦЕЛИКОМ, вместе со здоровыми соседями по батчу.
#
# Смысл батча не в экономии баллов (Директ считает их по элементам), а во
# времени: прогон обязан уложиться между кроном Э1 и сверкой, а при трёхстах
# действиях запрос за элементом в это окно уже не помещается.
BATCH_LIMIT = 10


class DirectWriteError(RuntimeError):
    """Ошибка записи. outcome_unknown отделяет «запрос точно не ушёл» от
    «исход неизвестен».

    Разница не косметическая. «Точно не ушло» — изменения в кабинете нет,
    действие переприменяется на следующем прогоне. «Исход неизвестен» —
    изменение МОЖЕТ БЫТЬ живым: строка обязана уйти в тот же контур, что и
    обрыв процесса после отправки ('stale'), иначе она не наблюдается
    сторожем, не оплачена риск-бюджетом, и diff следующего прогона её не
    предложит — фактическое состояние уже совпало с планом.
    """

    def __init__(self, service: str, code: Any, message: str, detail: str = "",
                 outcome_unknown: bool = False):
        self.service, self.code, self.detail = service, code, detail
        self.outcome_unknown = bool(outcome_unknown)
        super().__init__(f"{service}: [{code}] {message} {detail}".strip())


# Исключения транспорта, при которых запрос МОГ дойти до Директа: тело уже
# ушло, а ответа нет. Порядок проверки важен — ConnectTimeout наследует и
# ConnectionError, и Timeout, но означает как раз обратное: соединение не
# установилось, тело не отправлялось.
_REQUEST_NEVER_SENT = (
    requests.exceptions.ConnectTimeout,
    requests.exceptions.InvalidURL,
    requests.exceptions.MissingSchema,
    requests.exceptions.InvalidSchema,
    requests.exceptions.InvalidHeader,
)

_OUTCOME_UNKNOWN = (
    requests.exceptions.ReadTimeout,
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,     # разрыв, в т.ч. ПОСЛЕ отправки тела
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.RequestException,    # неопознанный сбой транспорта
)


def is_outcome_unknown(exc: BaseException) -> bool:
    """Мог ли запрос примениться в кабинете, несмотря на исключение.

    True — исход неизвестен (таймаут, разрыв, недоступность после ретраев).
    False — запрос точно не ушёл (соединение не установлено, ошибка до
    отправки) ИЛИ Директ явно ответил отказом уровня запроса.

    Умолчание — False: неизвестный тип исключения возникает до сети куда
    чаще, чем в момент отправки, и записывать всё подряд в живые изменения
    значит наполнить наблюдение и риск-бюджет фантомами. Транспортные
    случаи перечислены явно выше.
    """
    if isinstance(exc, DirectWriteError):
        return exc.outcome_unknown
    if isinstance(exc, _REQUEST_NEVER_SENT):
        return False
    return isinstance(exc, _OUTCOME_UNKNOWN)


def journal_writes_allowed(sandbox: bool, dry_run: bool) -> bool:
    """Единственное правило журнала: его состояние меняет только боевая запись.

    Журнал ОДИН на оба окружения — база одна. Поэтому право писать в него не
    равно праву писать в кабинет:
      * репетиция (dry_run) вообще ничего не отправляет, и переводить чужие
        строки в терминальные статусы от её имени нельзя: пометка «зависла»
        или «откат не удался» — это утверждение о боевом кабинете;
      * песочный клиент отправляет запросы в ПЕСОЧНИЦУ, а отметки оставлял бы
        в БОЕВОМ журнале — про действия, которых в боевом кабинете не было.

    Правило одно на оба рабочих процесса: и сторож (agent_e1_watchdog), и
    прямое применение (agent_e1) спрашивают его через эту функцию. Раньше
    сторож журнал в репетиции сознательно не трогал, а прогон применения
    ровно в той же репетиции переводил зависшие строки в 'stale' — два разных
    правила на один журнал.

    Собственные строки репетиции (статус 'dry_run', см. apply.py) под это
    правило не подпадают: они не меняют состояние ни одной боевой записи,
    описывают только сам прогон-репетицию и убираются по сроку хранения
    (db.purge_dry_run_actions).
    """
    return (not bool(sandbox)) and (not bool(dry_run))


def journal_allowed(client) -> bool:
    """То же правило, но по клиенту: можно ли ЕМУ писать в журнал действий.

    Признак песочницы обязателен: умолчания у него нет ни одного хорошего.
    False («клиент боевой») открывает боевой журнал прогону, про который мы
    не знаем, куда он вообще пишет; True («песочный») молча выключает журнал
    боевому прогону, и применённые действия остаются без следа. Отсутствие
    атрибута — это не режим работы, а сломанный клиент, и падать он обязан
    сразу, а не выбирать за нас один из двух неверных ответов.
    """
    if not hasattr(client, "sandbox"):
        raise AttributeError(
            "клиент без признака sandbox: правило журнала неопределимо — "
            "и «боевой», и «песочный» по умолчанию одинаково неверны"
        )
    return journal_writes_allowed(
        bool(client.sandbox),
        not bool(client.is_write_allowed()),
    )


# Коды ошибок уровня запроса, означающие «кабинет временно не принимает,
# повторите позже» — Директ ОТКЛОНИЛ вызов, ничего не применив. По справочнику
# ошибок API v5 (ref-v5/concepts/errors-list): 52 — сервер авторизации
# временно недоступен, 152 — недостаточно баллов, 506 — превышено ограничение
# на число соединений, 1000 — сервер временно недоступен, 1001 — ошибка
# инициализации сервиса. Это состояние КАБИНЕТА, а не дефект действия:
# жечь на нём счётчик попыток (как 'failed') значило бы доводить здоровые
# действия до потолка apply_attempts из-за чужой квоты.
TRANSIENT_QUOTA_CODES = (52, 152, 506, 1000, 1001)


def is_transient_quota(exc: BaseException) -> bool:
    """Отклонение из-за квоты/временной недоступности — переносится, не жжётся.

    Только ошибки уровня запроса с известным исходом: outcome_unknown-ошибки
    сюда не попадают принципиально — у них изменение может быть живым, и
    их контур ('stale') строже.
    """
    if not isinstance(exc, DirectWriteError) or exc.outcome_unknown:
        return False
    try:
        return int(exc.code) in TRANSIENT_QUOTA_CODES
    except (TypeError, ValueError):
        return False


def parse_units(header: str) -> Optional[int]:
    """Заголовок Units: «израсходовано/осталось/суточный лимит» → остаток."""
    parts = _units_parts(header)
    return parts[1] if parts else None


def parse_units_limit(header: str) -> Optional[int]:
    """Суточный лимит из того же заголовка Units."""
    parts = _units_parts(header)
    return parts[2] if parts else None


def _units_parts(header: str) -> Optional[tuple]:
    parts = (header or "").split("/")
    if len(parts) < 3:
        return None
    try:
        return tuple(int(p) for p in parts[:3])
    except ValueError:
        return None


class WriteClient:
    def __init__(self, account_login: str, sandbox: bool = True, dry_run: bool = True,
                 token: Optional[str] = None):
        self.login = account_login
        self.sandbox = sandbox
        self.dry_run = dry_run
        self.base = SANDBOX_BASE if sandbox else PROD_BASE
        self._token = token
        self.units_left: Optional[int] = None
        self.units_limit: Optional[int] = None

    def is_write_allowed(self) -> bool:
        return not self.dry_run

    def _resolve_token(self) -> str:
        token = self._token or os.environ.get("DIRECT_TOKEN")
        if not token:
            raise DirectWriteError("auth", "no_token", "DIRECT_TOKEN не задан в окружении")
        return token

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._resolve_token()}",
            "Client-Login": self.login,
            "Accept-Language": "ru",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _call(self, service: str, method: str, params: Dict[str, Any],
              retries: int) -> Dict[str, Any]:
        """Один вызов API. retries обязателен и БЕЗ умолчания.

        Умолчание здесь было бы тем же дефектом, что и захардкоженный
        absolute_max_cpa: новый вызывающий код молча получал бы повторы,
        которые для изменяющего запроса означают лишние объекты в кабинете.
        Забытый аргумент обязан уронить вызов.
        """
        if retries > 1 and method != "get":
            # Страховка от будущего вызывающего кода: повторять можно только
            # чтение. Проверка по методу, а не по флагу вызывающего, — флаг
            # можно передать неверно, метод врать не может.
            raise ValueError(
                f"изменяющий запрос не переотправляется: {service}.{method} "
                f"с retries={retries} создал бы в кабинете лишние объекты"
            )
        body = json.dumps({"method": method, "params": params}, ensure_ascii=False).encode("utf-8")
        headers = self._headers()
        for attempt in range(retries):
            resp = requests.post(f"{self.base}/{service}", data=body,
                                 headers=headers, timeout=120)
            # Директ отдаёт русские ошибки без charset — без этого текст нечитаем.
            resp.encoding = "utf-8"
            if resp.status_code in RETRY_CODES:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                # Последняя попытка и статус всё ещё retryable: тело могло быть
                # валидным JSON без ключа "error" (например, {} от балансировщика) —
                # такое нельзя разбирать как успех, иначе журнал действий пометит
                # мутацию applied, хотя Директ её не применил.
                # Исход НЕИЗВЕСТЕН, а не «не применилось»: запрос уходил в
                # Директ, 5xx мог прийти уже ПОСЛЕ применения — от
                # балансировщика, из-за таймаута на стороне сервиса, из-за
                # сбоя на ответе. Для изменяющего запроса попытка ровно одна
                # (WRITE_ATTEMPTS): повтор при таком исходе размножает объекты
                # в кабинете.
                note = ("сервис недоступен после ретраев" if retries > 1
                        else "сервис недоступен: изменяющий запрос не повторяется")
                raise DirectWriteError(service, resp.status_code, note,
                                       resp.text[:300], outcome_unknown=True)
            units = parse_units(resp.headers.get("Units", ""))
            if units is not None:
                self.units_left = units
            limit = parse_units_limit(resp.headers.get("Units", ""))
            if limit is not None:
                self.units_limit = limit
            try:
                data = resp.json()
            except ValueError:
                # Ответ пришёл, но это не JSON — что сделал Директ с запросом,
                # неизвестно. Считать «не применилось» нельзя.
                raise DirectWriteError(service, resp.status_code, "нераспознанный ответ",
                                       resp.text[:300], outcome_unknown=True)
            if "error" in data:
                # Ошибка уровня ЗАПРОСА: ключ error в теле. Ошибки уровня ЭЛЕМЕНТА
                # (result.*Results[].Errors) сюда не попадают — они не ошибка
                # запроса и должны остаться в result для разбора вызывающим кодом.
                err = data["error"]
                raise DirectWriteError(service, err.get("error_code"),
                                       err.get("error_string", ""),
                                       err.get("error_detail", ""))
            return data.get("result") or {}
        raise DirectWriteError(service, "retries", "исчерпаны попытки",
                               outcome_unknown=True)

    def get(self, service: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Чтение разрешено всегда — оно не меняет состояние.

        И повторяется при недоступности сервиса: ответ на повтор тот же, а
        сорванное чтение состояния кабинета останавливает весь прогон.
        """
        return self._call(service, "get", params, retries=READ_RETRIES)

    def mutate(self, service: str, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Изменение. В dry-run не отправляется: возвращается пометка.

        Отправляется РОВНО ОДИН раз (WRITE_ATTEMPTS). Недоступность сервиса
        или таймаут дают DirectWriteError с outcome_unknown=True — «исход
        неизвестен», а не «не применилось» и не «попробуем ещё раз».
        """
        if not self.is_write_allowed():
            return {"dry_run": True, "service": service, "method": method, "params": params}
        return self._call(service, method, params, retries=WRITE_ATTEMPTS)

    def mutate_batch(self, service: str, method: str, collection: str,
                     items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Несколько элементов ОДНИМ изменяющим запросом.

        collection — имя коллекции в теле («BidModifiers», «Campaigns»):
        выводить его из сервиса нельзя, у campaigns.update и bidmodifiers.add
        оно разное, а ошибка в имени означает запрос, который Директ примет
        как пустой.

        Различение уровней ошибки то же, что у одиночного вызова, и оно —
        причина, по которой батч вообще безопасен: ошибка ЗАПРОСА (ключ
        error в теле) поднимает исключение и означает, что не применилось
        НИЧЕГО; ошибка ЭЛЕМЕНТА приезжает в result.*Results[i].Errors и
        касается только своего элемента — соседи применены. Считай мы батч
        неделимым, один отклонённый элемент отменял бы девять применённых.

        Перебор элементов не отправляется вовсе: Директ отвергает такой
        запрос ЦЕЛИКОМ, то есть вместе со здоровыми соседями. Пустой батч —
        тоже отказ: это не «ничего не делаем», а трата балла и строка ошибки
        от Директа на запросе, которого никто не хотел.
        """
        listed = list(items)
        if not listed:
            raise ValueError(f"пустой батч {service}.{method}: отправлять нечего")
        if len(listed) > BATCH_LIMIT:
            raise ValueError(
                f"в один запрос {service}.{method} влезает не больше "
                f"{BATCH_LIMIT} элементов, передано {len(listed)}: "
                f"перебор Директ отвергает вместе со здоровыми соседями")
        return self.mutate(service, method, {collection: listed})
