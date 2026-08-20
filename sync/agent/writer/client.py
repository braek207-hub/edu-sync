# -*- coding: utf-8 -*-
"""
sync/agent/writer/client.py — транспорт записи в Яндекс Директ.

Формы вызовов взяты с рабочего d:\\vscode\\EDU кампании\\direct\\client.py: тот
репозиторий недоступен из CI, поэтому код здесь самодостаточный, но повторяет
проверенные на проде решения (ретраи на 5xx, учёт Units). Батчи не переносились —
здесь нет кода, который бы их использовал; появятся вместе с ним. Стиль запроса
согласован с sync/agent/segments.py (_api_headers/_api_post — тот же кабинет,
та же кодировка ответа).

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
from typing import Any, Dict, Optional

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
    """То же правило, но по клиенту: можно ли ЕМУ писать в журнал действий."""
    return journal_writes_allowed(
        bool(getattr(client, "sandbox", False)),
        not bool(client.is_write_allowed()),
    )


def parse_units(header: str) -> Optional[int]:
    """Заголовок Units: «израсходовано/осталось/суточный лимит»."""
    parts = (header or "").split("/")
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
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
