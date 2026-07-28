"""Async-клиент Yandex Metrika Logs API + чистые TSV/bucket хелперы (Фаза B, Task 2).

Сетевой флоу портирован из sync/logs_api_probe.py (проверенный рабочий сценарий:
create → poll(status=processed) → download part → clean). OAuth-заголовок и ретраи —
как sync/edu_visits.py:_metrica_get (429/500/502/503/504 → экспоненциальный backoff,
до 6 попыток). Сетевые функции намеренно НЕ юнит-тестируются (мок сети не даёт
ценности) — они покрыты интеграционным прогоном в Task 6. `parse_tsv`/`bucket_topn` —
чистые функции, тесты обязательны (см. tests/test_logs_api.py).
"""

import time
from typing import Dict, List, Set, Tuple

import requests

BASE = "https://api-metrika.yandex.net/management/v1/counter/{c}/logrequest"
CREATE = "https://api-metrika.yandex.net/management/v1/counter/{c}/logrequests"

_TRANSIENT_STATUSES = {429, 500, 502, 503, 504}
_TERMINAL_STATUSES = {"processed", "processing_failed", "awaiting_retry", "cleaned_by_user"}


def _headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"OAuth {token}"}


def _retry_request(method: str, url: str, token: str, *, params: Dict = None, timeout: int = 60) -> requests.Response:
    backoff = 2
    for attempt in range(6):
        resp = requests.request(method, url, params=params, headers=_headers(token), timeout=timeout)
        if resp.status_code == 200:
            return resp
        if resp.status_code in _TRANSIENT_STATUSES and attempt < 5:
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        raise RuntimeError(f"Logs API {method} {url} -> {resp.status_code}: {resp.text[:400]}")
    raise RuntimeError(f"Logs API {method} {url}: max retries")


def create_request(counter: str, date1: str, date2: str, fields: str, token: str) -> int:
    """POST logrequests — создаёт асинхронный запрос выгрузки визитов, возвращает request_id."""
    url = CREATE.format(c=counter)
    params = {"date1": date1, "date2": date2, "source": "visits", "fields": fields}
    resp = _retry_request("POST", url, token, params=params)
    return int(resp.json()["log_request"]["request_id"])


def _extract_part_numbers(status_json: Dict) -> List[int]:
    """Достаёт номера частей из ответа статуса Logs API.

    log_request.parts реально приходит списком СЛОВАРЕЙ {"part_number": N, "size": M}
    (не голых int, как можно было бы предположить по имени поля) — на этом упал
    прод-прогон Task 6: download_part получал dict вместо int и подставлял его как есть
    в URL (`partNumber={'part_number': 0, ...}`) → HTTP 400 у Метрики. Обрабатываем и
    dict, и уже-int на случай будущей смены формата API.
    """
    parts = (status_json or {}).get("log_request", {}).get("parts", []) or []
    numbers = [p["part_number"] if isinstance(p, dict) else int(p) for p in parts]
    return sorted(numbers)


def wait_processed(counter: str, req_id: int, token: str, poll_s: int = 10, max_polls: int = 60) -> List[int]:
    """Поллит статус до processed (или другого терминального), возвращает номера частей.

    Терминальные статусы кроме processed (processing_failed/awaiting_retry/cleaned_by_user)
    — ошибка, не "готово"; пробрасываем RuntimeError, вызывающий код решает что делать.
    """
    url = f"{BASE.format(c=counter)}/{req_id}"
    status = None
    parts: List[int] = []
    for _ in range(max_polls):
        resp = _retry_request("GET", url, token)
        status_json = resp.json()
        status = status_json.get("log_request", {}).get("status")
        parts = _extract_part_numbers(status_json)
        if status in _TERMINAL_STATUSES:
            break
        time.sleep(poll_s)
    if status != "processed":
        raise RuntimeError(f"Logs API request {req_id}: не дошли до processed (status={status})")
    return parts


def download_part(counter: str, req_id: int, part: int, token: str) -> str:
    """Скачивает одну часть выгрузки (TSV, как есть)."""
    url = f"{BASE.format(c=counter)}/{req_id}/part/{part}/download"
    resp = _retry_request("GET", url, token, timeout=180)
    return resp.text


def clean_request(counter: str, req_id: int, token: str) -> None:
    """POST clean — освобождает request_id на стороне Метрики после скачивания."""
    url = f"{BASE.format(c=counter)}/{req_id}/clean"
    _retry_request("POST", url, token)


def parse_tsv(tsv: str) -> Tuple[List[str], List[List[str]]]:
    """Разбирает TSV-выгрузку Logs API: первая строка — заголовок, остальные — строки."""
    lines = [line for line in tsv.split("\n") if line]
    if not lines:
        return [], []
    header = lines[0].split("\t")
    rows = [line.split("\t") for line in lines[1:]]
    return header, rows


def bucket_topn(value: str, allowed: Set[str], other: str = "other") -> str:
    """Схлопывает категориальное значение до top-N допустимых, иначе — bucket `other`."""
    if not value:
        return other
    return value if value in allowed else other
