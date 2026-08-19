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


class DirectWriteError(RuntimeError):
    def __init__(self, service: str, code: Any, message: str, detail: str = ""):
        self.service, self.code, self.detail = service, code, detail
        super().__init__(f"{service}: [{code}] {message} {detail}".strip())


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
              retries: int = 4) -> Dict[str, Any]:
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
                raise DirectWriteError(service, resp.status_code,
                                       "сервис недоступен после ретраев", resp.text[:300])
            units = parse_units(resp.headers.get("Units", ""))
            if units is not None:
                self.units_left = units
            try:
                data = resp.json()
            except ValueError:
                raise DirectWriteError(service, resp.status_code, "нераспознанный ответ",
                                       resp.text[:300])
            if "error" in data:
                # Ошибка уровня ЗАПРОСА: ключ error в теле. Ошибки уровня ЭЛЕМЕНТА
                # (result.*Results[].Errors) сюда не попадают — они не ошибка
                # запроса и должны остаться в result для разбора вызывающим кодом.
                err = data["error"]
                raise DirectWriteError(service, err.get("error_code"),
                                       err.get("error_string", ""),
                                       err.get("error_detail", ""))
            return data.get("result") or {}
        raise DirectWriteError(service, "retries", "исчерпаны попытки")

    def get(self, service: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Чтение разрешено всегда — оно не меняет состояние."""
        return self._call(service, "get", params)

    def mutate(self, service: str, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Изменение. В dry-run не отправляется: возвращается пометка."""
        if not self.is_write_allowed():
            return {"dry_run": True, "service": service, "method": method, "params": params}
        return self._call(service, method, params)
