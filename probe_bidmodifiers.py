# -*- coding: utf-8 -*-
"""
probe_bidmodifiers.py — какие формы принимает сервис bidmodifiers.

Урок Э0: формы запросов, написанные «по логике REST», стоили восьми упавших
прогонов. Пробуем в ПЕСОЧНИЦЕ, где ошибка ничего не стоит, и печатаем вердикт
по каждому варианту.

Проверяем:
  - bidmodifiers.get — какие поля возвращает и как называются наборы;
  - bidmodifiers.set — изменение процента у существующей корректировки;
  - bidmodifiers.add — создание корректировки (демография, устройство, регион);

Запуск: python probe_bidmodifiers.py [--prod]

Песочница боевым логинам недоступна (error 513 «логин не подключен», run 32217815538),
поэтому формы проверяются на проде — по заведомо несуществующим Id, которые ничего
не меняют. Метода toggle у сервиса нет (error 55), он из проб убран.
ENV: DIRECT_TOKEN, DIRECT_CLIENTS_JSON
"""

import json
import os
import sys
from typing import Any, Dict, List

import requests

SANDBOX = "https://api-sandbox.direct.yandex.com/json/v5"
PROD = "https://api.direct.yandex.com/json/v5"

_RIGHTS_CODES = {53, 54, 152, 513}

# Заведомо несуществующие идентификаторы: ошибка уровня элемента подтверждает
# форму запроса, ничего не меняя. Реальные объекты не затрагиваются.
NONEXISTENT_ID = 999_999_999


def classify(status: int, body: Dict[str, Any]) -> str:
    """Вердикт по ответу. Ошибка уровня ЭЛЕМЕНТА значит, что форма верна."""
    if status == 403:
        return "NO_ACCESS"
    error = body.get("error") or {}
    code = error.get("error_code")
    if code in _RIGHTS_CODES:
        return "NO_ACCESS"
    if code is not None:
        return "REJECTED"
    result = body.get("result") or {}
    if any(k.endswith("Results") for k in result) or result:
        return "OK"
    return "UNKNOWN"


def summarize(results: List[Dict[str, Any]]) -> str:
    return "\n".join(f"{r['case']:34} {r['verdict']}" for r in results)


def _login() -> str:
    raw = (os.environ.get("DIRECT_CLIENTS_JSON") or "").strip()
    if raw:
        for item in json.loads(raw):
            if isinstance(item, dict) and str(item.get("login", "")).strip():
                return item["login"]
    return os.environ["DIRECT_CLIENT_LOGIN"]


def _call(base: str, login: str, service: str, method: str, params: Dict[str, Any]) -> tuple:
    resp = requests.post(
        f"{base}/{service}",
        data=json.dumps({"method": method, "params": params}, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {os.environ['DIRECT_TOKEN']}",
            "Client-Login": login,
            "Accept-Language": "ru",
            "Content-Type": "application/json; charset=utf-8",
        },
        timeout=90,
    )
    resp.encoding = "utf-8"
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, body


def main() -> int:
    base = PROD if "--prod" in sys.argv else SANDBOX
    login = _login()
    print(f"кабинет {login}, база {base}\n")

    # Кампания для проб: любая существующая. В песочнице кабинет пустой,
    # поэтому ошибка «объект не найден» — нормальный и достаточный результат.
    status, body = _call(base, login, "campaigns", "get", {
        "SelectionCriteria": {}, "FieldNames": ["Id"], "Page": {"Limit": 1},
    })
    campaigns = ((body.get("result") or {}).get("Campaigns") or [])
    campaign_id = NONEXISTENT_ID
    print(f"кампания для проб: {campaign_id} (найдено {len(campaigns)})\n")

    cases: List[Dict[str, Any]] = []

    s, b = _call(base, login, "bidmodifiers", "get", {
        "SelectionCriteria": {"CampaignIds": [campaign_id]},
        "FieldNames": ["Id", "CampaignId", "AdGroupId", "Type", "Level"],
        "MobileAdjustmentFieldNames": ["BidModifier", "OperatingSystemType"],
        "DemographicsAdjustmentFieldNames": ["BidModifier", "Gender", "Age"],
    })
    cases.append({"case": "get без Levels", "verdict": classify(s, b),
                  "raw": json.dumps(b, ensure_ascii=False)[:300]})

    # Levels — обязательный параметр get (error 8000 без него). Проверяем значения.
    # Levels лежит ВНУТРИ SelectionCriteria: на верхнем уровне params он не виден,
    # и API продолжает жаловаться на его отсутствие.
    for levels in (["CAMPAIGN"], ["CAMPAIGN", "AD_GROUP"]):
        s, b = _call(base, login, "bidmodifiers", "get", {
            "SelectionCriteria": {"CampaignIds": [campaign_id], "Levels": levels},
            "FieldNames": ["Id", "CampaignId", "AdGroupId", "Type", "Level"],
            "MobileAdjustmentFieldNames": ["BidModifier", "OperatingSystemType"],
            "DemographicsAdjustmentFieldNames": ["BidModifier", "Gender", "Age"],
        })
        cases.append({"case": f"get Levels={levels}", "verdict": classify(s, b),
                      "raw": json.dumps(b, ensure_ascii=False)[:300]})

    s, b = _call(base, login, "bidmodifiers", "set", {
        "BidModifiers": [{"Id": NONEXISTENT_ID, "BidModifier": 110}],
    })
    cases.append({"case": "set по Id", "verdict": classify(s, b),
                  "raw": json.dumps(b, ensure_ascii=False)[:300]})

    s, b = _call(base, login, "bidmodifiers", "add", {
        "BidModifiers": [{
            "CampaignId": campaign_id,
            "DemographicsAdjustments": [{"Gender": "GENDER_MALE", "BidModifier": 110}],
        }],
    })
    cases.append({"case": "add demographics", "verdict": classify(s, b),
                  "raw": json.dumps(b, ensure_ascii=False)[:300]})

    s, b = _call(base, login, "bidmodifiers", "add", {
        "BidModifiers": [{
            "CampaignId": campaign_id,
            "MobileAdjustment": {"BidModifier": 110},
        }],
    })
    cases.append({"case": "add mobile", "verdict": classify(s, b),
                  "raw": json.dumps(b, ensure_ascii=False)[:300]})

    print(summarize(cases))
    print("\nсырые ответы:")
    for c in cases:
        print(f"\n--- {c['case']} ---\n{c['raw']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
