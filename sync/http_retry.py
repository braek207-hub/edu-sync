"""HTTP-сессия с таймаутом и повтором на временных сбоях — для чтения из API.

Без таймаута requests ждёт ответа вечно: подвисшее соединение с Директом держит
systemd-юнит синка до ручного kill, и следующий запуск по таймеру не стартует.

Повторяются только 429/5xx и сетевые обрывы. 201/202/204 Reports API и 400/56 сюда
не входят — их ожидание по retryIn живёт в вызывающем коде (direct_reports_limit).
POST в списке разрешённых методов осознанно: у Директа чтение — это POST с method=get,
а Reports идемпотентен по ReportName. Для мутирующих вызовов (add/update/delete)
эта сессия не годится — повтор после обрыва на чтении ответа задвоит запись.
"""
from __future__ import annotations

from typing import Any, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# connect — дойти до api.direct.yandex.com; read — онлайн-отчёт Reports API Директ
# держит до ответа 201 порядка минуты, большие TSV отдаёт дольше: 300 с с запасом.
DEFAULT_TIMEOUT: Tuple[float, float] = (10, 300)
RETRY_STATUSES = (429, 500, 502, 503, 504)


class _TimeoutSession(requests.Session):
    def __init__(self, timeout: Tuple[float, float]):
        super().__init__()
        self.timeout = timeout

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        return super().request(method, url, **kwargs)


def read_only_session(
    timeout: Tuple[float, float] = DEFAULT_TIMEOUT,
    total: int = 5,
    backoff_factor: float = 2.0,
) -> requests.Session:
    retry = Retry(
        total=total,
        connect=total,
        read=total,
        status=total,
        backoff_factor=backoff_factor,
        status_forcelist=RETRY_STATUSES,
        allowed_methods=frozenset({"GET", "POST"}),
        respect_retry_after_header=True,
        # После исчерпания отдаём последний ответ как есть: вызывающий код сам
        # поднимет RuntimeError с телом ошибки Директа, а не безликий RetryError.
        raise_on_status=False,
    )
    session = _TimeoutSession(timeout)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
