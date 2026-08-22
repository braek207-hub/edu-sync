# -*- coding: utf-8 -*-
"""
probe_device_conflict.py — можно ли задать несколько корректировок устройств.

Что уже установлено:
  · 32559366898 — TABLET поверх только что созданного DESKTOP отвергнут:
    Code 6000 «Условия в корректировках пересекаются»;
  · 32561294615 — TABLET отвергнут и в паре с нейтральным MOBILE (DESKTOP при
    этом УЖЕ СТОЯЛ в кампании отдельно), MOBILE в том же запросе принят;
  · 32561534117 — TABLET в кампанию БЕЗ десктопной корректировки принят.

Слабое место этих опытов: во всех случаях десктопная корректировка либо уже
существовала, либо отсутствовала — но ни разу DESKTOP и TABLET не отправлялись
ВМЕСТЕ, одним запросом, в чистую кампанию. Именно это и надо проверить, прежде
чем утверждать «одновременно нельзя»: возможно, нельзя лишь ДОБАВЛЯТЬ вторую
к существующей, а согласованный набор проходит.

Опыт: кампания без корректировок устройств, нейтральные значения (100 — ставок
не меняют), три варианта по возрастанию:
  A) DESKTOP + TABLET одним запросом;
  B) MOBILE + DESKTOP + TABLET одним запросом;
  C) те же по одной, последовательно — контроль.
После каждого варианта состояние перечитывается и всё созданное удаляется:
Id в ответе add приходит пустым (побочная находка 32561534117), поэтому уборка
идёт только перечитыванием.

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
DEVICE_TYPES = ("MOBILE_ADJUSTMENT", "DESKTOP_ADJUSTMENT", "TABLET_ADJUSTMENT")
FIELD = {"MOBILE_ADJUSTMENT": "MobileAdjustment",
         "DESKTOP_ADJUSTMENT": "DesktopAdjustment",
         "TABLET_ADJUSTMENT": "TabletAdjustment"}


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
            out.append("ОТКАЗ " + "; ".join(
                f"{e.get('Code')} {e.get('Details') or e.get('Message')}" for e in errors))
        else:
            out.append("ПРИНЯТО")
    return " | ".join(out) or json.dumps(result, ensure_ascii=False)[:300]


def _campaign_ids(login, limit=100):
    result = _post(CAMPAIGNS_URL, login, {
        "method": "get",
        "params": {"SelectionCriteria": {"States": ["ON", "OFF", "SUSPENDED"]},
                   "FieldNames": ["Id"], "Page": {"Limit": limit, "Offset": 0}},
    })
    return [int(c["Id"]) for c in (result.get("result") or {}).get("Campaigns", [])]


def _device_state(login, campaign_ids):
    """Корректировки устройств по кампаниям: {campaign_id: {type: id}}."""
    result = _post(BIDMODIFIERS_URL, login, {
        "method": "get",
        "params": {
            "SelectionCriteria": {"CampaignIds": campaign_ids, "Levels": ["CAMPAIGN"]},
            "FieldNames": ["Id", "CampaignId", "Type"],
        },
    })
    out = {}
    for item in (result.get("result") or {}).get("BidModifiers", []):
        if item.get("Type") in DEVICE_TYPES:
            out.setdefault(int(item["CampaignId"]), {})[item["Type"]] = item.get("Id")
    return out


def _cleanup(login, campaign_id):
    """Удаляет ВСЕ корректировки устройств кампании — по перечитыванию, а не по
    Id из ответа add: тот приходит пустым."""
    state = _device_state(login, [campaign_id]).get(campaign_id, {})
    ids = [i for i in state.values() if i]
    if not ids:
        return "убирать нечего"
    result = _post(BIDMODIFIERS_URL, login, {
        "method": "delete", "params": {"SelectionCriteria": {"Ids": ids}}})
    return f"удалено {ids}: {_verdict(result, 'DeleteResults')}"


def _item(campaign_id, direct_type):
    return {"CampaignId": campaign_id, FIELD[direct_type]: {"BidModifier": NEUTRAL}}


def main() -> int:
    apply = "--apply" in sys.argv

    ids = _campaign_ids(ACCOUNT)
    state = _device_state(ACCOUNT, ids)
    clean = [cid for cid in ids if not state.get(cid)]
    print(f"кампаний не в архиве: {len(ids)}; без корректировок устройств: {len(clean)}")
    if not clean:
        print("нет чистой кампании — опыт невозможен")
        return 0

    target = clean[0]
    print(f"подопытная кампания: {target} (значения нейтральные, ставок не меняют)")
    if not apply:
        print("\nРепетиция: ничего не отправлено. Нужен --apply.")
        return 0

    print(f"\n--- A: DESKTOP + TABLET одним запросом")
    res = _post(BIDMODIFIERS_URL, ACCOUNT, {"method": "add", "params": {"BidModifiers": [
        _item(target, "DESKTOP_ADJUSTMENT"), _item(target, "TABLET_ADJUSTMENT")]}})
    print("  " + _verdict(res))
    print("  состояние после: " + json.dumps(
        _device_state(ACCOUNT, [target]).get(target, {}), ensure_ascii=False))
    print("  " + _cleanup(ACCOUNT, target))

    print(f"\n--- B: MOBILE + DESKTOP + TABLET одним запросом")
    res = _post(BIDMODIFIERS_URL, ACCOUNT, {"method": "add", "params": {"BidModifiers": [
        _item(target, t) for t in DEVICE_TYPES]}})
    print("  " + _verdict(res))
    print("  состояние после: " + json.dumps(
        _device_state(ACCOUNT, [target]).get(target, {}), ensure_ascii=False))
    print("  " + _cleanup(ACCOUNT, target))

    print(f"\n--- C: по одной, последовательно (контроль)")
    for direct_type in ("DESKTOP_ADJUSTMENT", "TABLET_ADJUSTMENT"):
        res = _post(BIDMODIFIERS_URL, ACCOUNT, {
            "method": "add", "params": {"BidModifiers": [_item(target, direct_type)]}})
        print(f"  {direct_type}: {_verdict(res)}")
    print("  состояние после: " + json.dumps(
        _device_state(ACCOUNT, [target]).get(target, {}), ensure_ascii=False))
    print("  " + _cleanup(ACCOUNT, target))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
