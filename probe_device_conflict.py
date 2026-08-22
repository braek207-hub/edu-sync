# -*- coding: utf-8 -*-
"""
probe_device_conflict.py — с чем именно пересекается TABLET_ADJUSTMENT.

Первая боевая запись (32559366898): шесть корректировок применились, седьмая —
TABLET — отвергнута с Code 6000 «Условия в корректировках пересекаются».
Документация Яндекса утверждает, что DESKTOP (компьютеры + Smart TV) и TABLET
не взаимоисключающие. Практика говорит иначе; верим кабинету.

Прогон 32561294615 ОПРОВЕРГ первую гипотезу («набор устройств надо слать
целиком»): TABLET отвергнут и в паре с нейтральным MOBILE, причём сам MOBILE
в том же запросе был принят. Значит пересечение — именно с DESKTOP,
единственной корректировкой устройств, которая в кампании уже стоит.

Проверка чистая и обратимая: добавить НЕЙТРАЛЬНЫЙ TABLET (100 — ставок не
меняет) в кампанию того же кабинета, где DESKTOP-корректировки НЕТ, и сразу
удалить. Пройдёт — конфликт именно с DESKTOP подтверждён, и трогать боевую
кампанию для этого не потребовалось.

Без --apply ничего не отправляет.
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


def _campaign_ids(login, limit=40):
    result = _post(CAMPAIGNS_URL, login, {
        "method": "get",
        "params": {"SelectionCriteria": {}, "FieldNames": ["Id"],
                   "Page": {"Limit": limit, "Offset": 0}},
    })
    return [int(c["Id"]) for c in (result.get("result") or {}).get("Campaigns", [])]


def _device_modifiers(login, campaign_ids):
    """Какие корректировки устройств стоят у кампаний."""
    result = _post(BIDMODIFIERS_URL, login, {
        "method": "get",
        "params": {
            "SelectionCriteria": {"CampaignIds": campaign_ids, "Levels": ["CAMPAIGN"]},
            "FieldNames": ["Id", "CampaignId", "Type"],
        },
    })
    by_campaign = {}
    for item in (result.get("result") or {}).get("BidModifiers", []):
        by_campaign.setdefault(int(item["CampaignId"]), set()).add(item.get("Type"))
    return by_campaign


def main() -> int:
    apply = "--apply" in sys.argv
    if not apply:
        print("Репетиция: ничего не отправлено. Нужен --apply.")
        return 0

    ids = _campaign_ids(ACCOUNT)
    types = _device_modifiers(ACCOUNT, ids)

    clean = [cid for cid in ids
             if "DESKTOP_ADJUSTMENT" not in types.get(cid, set())
             and "TABLET_ADJUSTMENT" not in types.get(cid, set())]
    withdesktop = [cid for cid in ids if "DESKTOP_ADJUSTMENT" in types.get(cid, set())]
    print(f"кампаний просмотрено: {len(ids)}; "
          f"без DESKTOP-корректировки: {len(clean)}; с DESKTOP: {len(withdesktop)}")

    if not clean:
        print("не нашлось кампании без DESKTOP-корректировки — эксперимент невозможен")
        return 0

    target = clean[0]
    print(f"\n--- нейтральный TABLET (100) в кампанию {target} БЕЗ DESKTOP-корректировки")
    added = _post(BIDMODIFIERS_URL, ACCOUNT, {"method": "add", "params": {"BidModifiers": [
        {"CampaignId": target, "TabletAdjustment": {"BidModifier": NEUTRAL}}]}})
    print("  " + _verdict(added))

    # Убираем за собой независимо от исхода: корректировка нейтральная, но
    # оставлять после себя следы эксперимента в боевом кабинете нельзя.
    new_id = None
    for element in ((added.get("result") or {}).get("AddResults") or []):
        if not element.get("Errors"):
            new_id = element.get("Id")
    if new_id:
        deleted = _post(BIDMODIFIERS_URL, ACCOUNT, {
            "method": "delete", "params": {"SelectionCriteria": {"Ids": [new_id]}}})
        print(f"  уборка: удаление Id={new_id} → {_verdict(deleted, 'DeleteResults')}")
    else:
        print("  уборка не нужна: корректировка не создана")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
