# -*- coding: utf-8 -*-
"""
probe_device_conflict.py — DESKTOP и TABLET в Директе несовместимы.

Установлено экспериментом, вопреки документации:

  · первая боевая запись (32559366898): TABLET отвергнут с Code 6000 «Условия
    в корректировках пересекаются» — в кампании уже стоял DESKTOP;
  · 32561294615: гипотеза «набор устройств надо слать целиком» ОПРОВЕРГНУТА —
    TABLET отвергнут и в паре с нейтральным MOBILE, причём MOBILE приняли;
  · 32561534117: тот же TABLET в кампанию БЕЗ DESKTOP-корректировки — ПРИНЯТ.

Вывод: пересекается TABLET именно с DESKTOP. Справочник Яндекса утверждает
обратное («компьютеры, Smart TV» против «планшеты» — разные категории), но
кабинет отвечает иначе, и прав кабинет.

Побочная находка: bidmodifiers.add вернул успех с Id = null. Уборка по Id из
ответа поэтому не сработала — след эксперимента пришлось искать перечитыванием.
Тот же дефект отмечен в движке записи как отложенный (read-back после
неизвестного исхода): полагаться на Id из ответа add нельзя.

Режим по умолчанию — уборка: найти нейтральные TABLET-корректировки и удалить.
Запуск: python probe_device_conflict.py [--apply]
ENV: DIRECT_TOKEN, DIRECT_CLIENTS_JSON
"""

import json
import os
import sys

import requests

BIDMODIFIERS_URL = "https://api.direct.yandex.com/json/v5/bidmodifiers"
CAMPAIGNS_URL = "https://api.direct.yandex.com/json/v5/campaigns"

ACCOUNT = "account10-506462-fqs4"
NEUTRAL = 100


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


def _verdict(result, collection="AddResults"):
    if "_http" in result:
        return f"HTTP {result['_http']}: {result['_body']}"
    error = result.get("error") or {}
    if error:
        return (f"ошибка запроса: {error.get('error_code')} "
                f"{error.get('error_string')} / {error.get('error_detail')}")
    items = (result.get("result") or {}).get(collection) or []
    out = []
    for element in items:
        errors = element.get("Errors") or []
        if errors:
            out.append("ОТКАЗ: " + "; ".join(
                f"{e.get('Code')} {e.get('Details') or e.get('Message')}" for e in errors))
        else:
            out.append(f"ПРИНЯТО, Id={element.get('Id')}")
    return " | ".join(out) or json.dumps(result, ensure_ascii=False)[:300]


def _campaign_ids(login, limit=100):
    """Только НЕ заархивированные: архив править запрещено (ошибка 8300)."""
    result = _post(CAMPAIGNS_URL, login, {
        "method": "get",
        "params": {
            "SelectionCriteria": {"States": ["ON", "OFF", "SUSPENDED"]},
            "FieldNames": ["Id"],
            "Page": {"Limit": limit, "Offset": 0},
        },
    })
    return [int(c["Id"]) for c in (result.get("result") or {}).get("Campaigns", [])]


def _tablet_modifiers(login, campaign_ids):
    """Планшетные корректировки с их значениями — для поиска следов probe."""
    result = _post(BIDMODIFIERS_URL, login, {
        "method": "get",
        "params": {
            "SelectionCriteria": {"CampaignIds": campaign_ids, "Levels": ["CAMPAIGN"]},
            "FieldNames": ["Id", "CampaignId", "Type"],
            "TabletAdjustmentFieldNames": ["BidModifier"],
        },
    })
    out = []
    for item in (result.get("result") or {}).get("BidModifiers", []):
        if item.get("Type") == "TABLET_ADJUSTMENT":
            out.append({
                "Id": item.get("Id"),
                "CampaignId": item.get("CampaignId"),
                "BidModifier": (item.get("TabletAdjustment") or {}).get("BidModifier"),
            })
    return out


def main() -> int:
    apply = "--apply" in sys.argv

    ids = _campaign_ids(ACCOUNT)
    tablets = _tablet_modifiers(ACCOUNT, ids)
    print(f"кампаний не в архиве: {len(ids)}; планшетных корректировок: {len(tablets)}")
    for item in tablets:
        print("  " + json.dumps(item, ensure_ascii=False))

    # Нейтральная планшетная корректировка ставок не меняет, но это след
    # эксперимента в боевом кабинете, и его надо убрать.
    litter = [t for t in tablets if t["BidModifier"] == NEUTRAL and t["Id"]]
    if not litter:
        print("следов probe не найдено — убирать нечего")
        return 0

    print(f"\nк удалению: {[t['Id'] for t in litter]}")
    if not apply:
        print("(репетиция, ничего не удалено — нужен --apply)")
        return 0

    deleted = _post(BIDMODIFIERS_URL, ACCOUNT, {
        "method": "delete",
        "params": {"SelectionCriteria": {"Ids": [t["Id"] for t in litter]}},
    })
    print("  " + _verdict(deleted, "DeleteResults"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
