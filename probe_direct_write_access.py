# -*- coding: utf-8 -*-
"""
probe_direct_write_access.py — разовый probe перед Э1 автопилота Директа.

Проверяет ДВЕ вещи:
  1. Есть ли у DIRECT_TOKEN права на ЗАПИСЬ.
  2. Доступен ли сервис A/B-экспериментов через API v5 (в справочнике его нет,
     но отсутствие в выдаче — не доказательство).

Как проверяются права на запись, не трогая боевые кампании:
    Отправляем campaigns.update на ЗАВЕДОМО НЕСУЩЕСТВУЮЩИЙ Id.
      - прав нет  → ошибка уровня ЗАПРОСА (ключ "error", коды 54/152/513) или HTTP 403;
      - права есть → HTTP 200 с result.UpdateResults[i].Errors — ошибка уровня ЭЛЕМЕНТА
        («объект не найден»), то есть запрос прошёл авторизацию.
    Наличие UpdateResults в ответе и есть доказательство права на запись.

Режим --real делает no-op update реальной кампании (записывает её же имя) — нужен
только если safe-режим дал UNKNOWN.

Запуск:
    python probe_direct_write_access.py              # безопасный режим
    python probe_direct_write_access.py --real       # no-op на реальной кампании

ENV: DIRECT_TOKEN, DIRECT_CLIENTS_JSON (или DIRECT_CLIENT_LOGIN)
"""

import json
import os
import sys
from typing import Any, Dict, List, Optional

import requests

CAMPAIGNS_URL = "https://api.direct.yandex.com/json/v5/campaigns"
EXPERIMENTS_URL = "https://api.direct.yandex.com/json/v5/experiments"

# Заведомо несуществующий Id: диапазон реальных кампаний Директа много ниже.
NONEXISTENT_CAMPAIGN_ID = 999_999_999

# Коды ошибок API Директа, означающие отсутствие прав/доступа.
_RIGHTS_ERROR_CODES = {54, 152, 513}
_NO_SERVICE_ACCESS_CODES = {53, 58}


def classify_write_response(status: int, body: Dict[str, Any]) -> str:
    """Вердикт по ответу на пробную запись.

    WRITE_OK  — в ответе есть UpdateResults: запрос прошёл авторизацию.
                Ошибки внутри элементов («не найден») праву на запись не мешают.
    READ_ONLY — ошибка уровня запроса с кодом прав.
    NO_RIGHTS — HTTP 403.
    UNKNOWN   — форма ответа не распознана.
    """
    if status == 403:
        return "NO_RIGHTS"
    error = body.get("error") or {}
    code = error.get("error_code")
    if code in _RIGHTS_ERROR_CODES:
        return "READ_ONLY"
    if code is not None:
        return "UNKNOWN"
    result = body.get("result") or {}
    if "UpdateResults" in result:
        return "WRITE_OK"
    return "UNKNOWN"


def experiments_endpoint_verdict(status: int, body: Dict[str, Any]) -> str:
    """Вердикт по доступности сервиса экспериментов."""
    if status == 404:
        return "ABSENT"
    error = body.get("error") or {}
    code = error.get("error_code")
    if code in _NO_SERVICE_ACCESS_CODES:
        return "NO_ACCESS"
    if code is not None:
        return "UNKNOWN"
    if isinstance(body.get("result"), dict):
        return "AVAILABLE"
    return "UNKNOWN"


def _headers(login: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {os.environ['DIRECT_TOKEN']}",
        "Client-Login": login,
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
    }


def _post(url: str, login: str, payload: Dict[str, Any]) -> tuple:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    resp = requests.post(url, data=data, headers=_headers(login), timeout=60)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, body


def _logins() -> List[str]:
    raw = os.environ.get("DIRECT_CLIENTS_JSON")
    if raw:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return list(parsed.values())
        return [str(x) for x in parsed]
    return [os.environ["DIRECT_CLIENT_LOGIN"]]


def probe(login: str, real_write: bool = False) -> Dict[str, Any]:
    out: Dict[str, Any] = {"login": login}

    # 1. Чтение — заодно подтверждает, что токен вообще рабочий для этого логина.
    status_r, body_r = _post(
        CAMPAIGNS_URL,
        login,
        {
            "method": "get",
            "params": {
                "SelectionCriteria": {"States": ["ON", "SUSPENDED"]},
                "FieldNames": ["Id", "Name", "State", "Status"],
                "Page": {"Limit": 3},
            },
        },
    )
    campaigns = ((body_r.get("result") or {}).get("Campaigns") or [])
    out["read"] = "OK" if status_r == 200 and campaigns else f"FAIL http={status_r}"
    out["campaigns_seen"] = len(campaigns)
    if status_r != 200:
        out["read_raw"] = json.dumps(body_r, ensure_ascii=False)[:300]

    # 2. Права на запись.
    if real_write and campaigns:
        camp = campaigns[0]
        write_payload = {
            "method": "update",
            "params": {"Campaigns": [{"Id": camp["Id"], "Name": camp["Name"]}]},
        }
        out["write_mode"] = f"real no-op на кампании {camp['Id']}"
    else:
        write_payload = {
            "method": "update",
            "params": {"Campaigns": [{"Id": NONEXISTENT_CAMPAIGN_ID, "Name": "probe"}]},
        }
        out["write_mode"] = f"safe: несуществующий Id {NONEXISTENT_CAMPAIGN_ID}"

    status_w, body_w = _post(CAMPAIGNS_URL, login, write_payload)
    out["write"] = classify_write_response(status_w, body_w)
    out["write_http"] = status_w
    out["write_raw"] = json.dumps(body_w, ensure_ascii=False)[:400]

    # 3. Эксперименты — только чтение.
    status_e, body_e = _post(
        EXPERIMENTS_URL,
        login,
        {"method": "get", "params": {"FieldNames": ["Id", "Name", "Status"]}},
    )
    out["experiments"] = experiments_endpoint_verdict(status_e, body_e)
    out["experiments_http"] = status_e
    out["experiments_raw"] = json.dumps(body_e, ensure_ascii=False)[:400]

    return out


def main() -> int:
    real_write = "--real" in sys.argv
    results = []
    for login in _logins():
        try:
            results.append(probe(login, real_write=real_write))
        except Exception as exc:  # probe не должен падать на одном логине из нескольких
            results.append({"login": login, "error": f"{type(exc).__name__}: {exc}"})
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
