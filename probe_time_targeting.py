# -*- coding: utf-8 -*-
"""
probe_time_targeting.py — как в кабинете устроен временной таргетинг.

Э0 считает почасовой профиль (достижения целей по часам из Метрики) и кладёт
23 строки вида schedule:hour в edu_agent_computed_settings. Движок записи их
не применяет: у корректировок ставок и у расписания РАЗНЫЕ механизмы —
bidmodifiers против TimeTargeting внутри самой кампании. На боевом прогоне
32558338766 эти 23 строки честно ушли в unsupported.

Перед тем как писать применение, надо увидеть настоящую форму, а не описание
из справочника: приходит ли TimeTargeting в campaigns.get, как выглядит
HoursBids, что стоит сейчас у живых кампаний и в какой шкале.

Только чтение. Запуск: python probe_time_targeting.py
ENV: DIRECT_TOKEN, DIRECT_CLIENTS_JSON
"""

import json
import os

import requests

CAMPAIGNS_URL = "https://api.direct.yandex.com/json/v5/campaigns"


def _logins():
    raw = (os.environ.get("DIRECT_CLIENTS_JSON") or "").strip()
    out = []
    if raw:
        for item in json.loads(raw):
            login = item.get("login") if isinstance(item, dict) else item
            if login:
                out.append(str(login))
    return out


def _post(login, payload):
    headers = {
        "Authorization": f"Bearer {os.environ['DIRECT_TOKEN']}",
        "Client-Login": login,
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    r = requests.post(CAMPAIGNS_URL, data=body, headers=headers, timeout=120)
    if r.status_code != 200:
        return {"_http": r.status_code, "_body": r.text[:400]}
    return r.json()


def main() -> int:
    for login in _logins():
        print(f"\n{'=' * 70}\nКАБИНЕТ {login}")

        # TimeTargeting лежит в общих настройках кампании (FieldNames), а не в
        # блоке типа — но проверяем оба, чтобы не гадать.
        body = {
            "method": "get",
            "params": {
                "SelectionCriteria": {},
                "FieldNames": ["Id", "Name", "TimeTargeting", "TimeZone"],
                "Page": {"Limit": 5, "Offset": 0},
            },
        }
        result = _post(login, body)
        if "_http" in result:
            print(f"FieldNames=TimeTargeting → HTTP {result['_http']}: {result['_body']}")
        else:
            error = (result.get("error") or {})
            if error:
                print(f"ошибка API: {error.get('error_code')} "
                      f"{error.get('error_string')} / {error.get('error_detail')}")
            for campaign in (result.get("result") or {}).get("Campaigns", [])[:5]:
                print(json.dumps(campaign, ensure_ascii=False, indent=2)[:1200])
                print("-" * 50)
        break  # одного кабинета достаточно, чтобы увидеть форму

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
