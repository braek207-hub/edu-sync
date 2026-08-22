# -*- coding: utf-8 -*-
"""
probe_time_targeting.py — форма TimeTargeting и состав корректировок кампании.

Два открытых вопроса после первой боевой записи (прогон 32559366898):

1. Расписание. Э0 считает почасовой профиль (достижения целей по часам из
   Метрики) и кладёт 23 строки schedule:hour, а движок записи их не применяет:
   у корректировок ставок и у расписания РАЗНЫЕ механизмы — bidmodifiers
   против TimeTargeting внутри самой кампании. Перед тем как писать
   применение, надо увидеть настоящую форму ответа, а не описание справочника.

2. Отклонение. TABLET_ADJUSTMENT для кампании 114057545 отвергнут с
   Code 6000 «Условия в корректировках пересекаются». Движок считал, что
   такой корректировки нет, и послал add. Смотрим, что в кампании ЛЕЖИТ —
   и чем планшет пересекается с уже применёнными шестью.

Только чтение. Запуск: python probe_time_targeting.py
ENV: DIRECT_TOKEN, DIRECT_CLIENTS_JSON
"""

import json
import os

import requests

CAMPAIGNS_URL = "https://api.direct.yandex.com/json/v5/campaigns"
BIDMODIFIERS_URL = "https://api.direct.yandex.com/json/v5/bidmodifiers"

# Кампания, на которой прошла первая боевая запись.
REJECTED_CAMPAIGN = 114057545
REJECTED_ACCOUNT = "account10-506462-fqs4"


def _logins():
    raw = (os.environ.get("DIRECT_CLIENTS_JSON") or "").strip()
    out = []
    if raw:
        for item in json.loads(raw):
            login = item.get("login") if isinstance(item, dict) else item
            if login:
                out.append(str(login))
    return out


def _post(url, login, payload):
    headers = {
        "Authorization": f"Bearer {os.environ['DIRECT_TOKEN']}",
        "Client-Login": login,
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    r = requests.post(url, data=body, headers=headers, timeout=120)
    if r.status_code != 200:
        return {"_http": r.status_code, "_body": r.text[:400]}
    return r.json()


def _show_error(result):
    error = result.get("error") or {}
    if error:
        print(f"  ошибка API: {error.get('error_code')} {error.get('error_string')} "
              f"/ {error.get('error_detail')}")
        return True
    return False


def probe_time_targeting(login):
    print(f"\n--- TimeTargeting, кабинет {login}")
    result = _post(CAMPAIGNS_URL, login, {
        "method": "get",
        "params": {
            "SelectionCriteria": {},
            "FieldNames": ["Id", "Name", "TimeTargeting", "TimeZone"],
            "Page": {"Limit": 3, "Offset": 0},
        },
    })
    if "_http" in result:
        print(f"  HTTP {result['_http']}: {result['_body']}")
        return
    if _show_error(result):
        return
    for campaign in (result.get("result") or {}).get("Campaigns", [])[:3]:
        print(json.dumps(campaign, ensure_ascii=False, indent=2)[:1500])
        print("  " + "-" * 48)


def probe_modifiers(login, campaign_id):
    print(f"\n--- корректировки кампании {campaign_id}")
    result = _post(BIDMODIFIERS_URL, login, {
        "method": "get",
        "params": {
            "SelectionCriteria": {"CampaignIds": [int(campaign_id)],
                                  "Levels": ["CAMPAIGN"]},
            "FieldNames": ["Id", "CampaignId", "Type", "Level"],
            "MobileAdjustmentFieldNames": ["BidModifier", "OperatingSystemType"],
            "DesktopAdjustmentFieldNames": ["BidModifier"],
            "TabletAdjustmentFieldNames": ["BidModifier", "OperatingSystemType"],
            "DemographicsAdjustmentFieldNames": ["BidModifier", "Gender", "Age"],
        },
    })
    if "_http" in result:
        print(f"  HTTP {result['_http']}: {result['_body']}")
        return
    if _show_error(result):
        return
    items = (result.get("result") or {}).get("BidModifiers") or []
    print(f"  всего корректировок: {len(items)}")
    for item in items:
        print("  " + json.dumps(item, ensure_ascii=False))


def main() -> int:
    logins = _logins()
    for login in logins:
        print(f"\n{'=' * 70}\nКАБИНЕТ {login}")
        probe_time_targeting(login)
        if login == REJECTED_ACCOUNT:
            probe_modifiers(login, REJECTED_CAMPAIGN)
        break  # формы одинаковы во всех кабинетах

    # Кабинет отклонённого действия мог не оказаться первым.
    if REJECTED_ACCOUNT in logins and logins[0] != REJECTED_ACCOUNT:
        print(f"\n{'=' * 70}\nКАБИНЕТ {REJECTED_ACCOUNT} (отклонённое действие)")
        probe_modifiers(REJECTED_ACCOUNT, REJECTED_CAMPAIGN)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
