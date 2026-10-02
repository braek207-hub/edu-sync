"""Что реально лежит в объявлениях кампании-образца 714000003.

Сборщик копировал только Types=["TEXT_AD"] с одиночными Title/Title2/Text и одной
картинкой — поэтому в клоне 714996447 нет ни набора заголовков, ни цены, ни карусели.
Снимаем фактический состав: какие типы и подтипы объявлений в образце и какие блоки
полей их описывают. Имена блоков не угадываем — перебираем кандидатов и печатаем ответ
API на каждый. Только чтение.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.direct.yandex.com/json/v5/"
SOURCE = 714000003
CLONE = 714996447
EXPECTED_LOGIN_PART = "bjorn"

BLOCKS = {
    "TextAdFieldNames": ["Title", "Title2", "Text", "Href", "Mobile", "DisplayUrlPath",
                         "AdImageHash", "SitelinkSetId", "VCardId", "AdExtensions",
                         "VideoExtension", "TurboPageId", "BusinessId", "PreferVCardOverBusiness"],
    "TextImageAdFieldNames": ["AdImageHash", "Href", "TurboPageId"],
    "CpcVideoAdBuilderAdFieldNames": ["Creative", "Href", "TurboPageId"],
    "SmartAdBuilderAdFieldNames": ["Creative", "TurboPageId"],
}


def call(service: str, params: dict, login: str, token: str) -> dict:
    body = json.dumps({"method": "get", "params": params}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API + service, data=body, headers={
        "Authorization": "Bearer " + token, "Client-Login": login,
        "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode())


def main() -> None:
    token = os.environ.get("DIRECT_TOKEN", "").strip()
    login = ""
    for item in json.loads(os.environ.get("DIRECT_CLIENTS_JSON") or "[]"):
        cand = item.get("login") or item.get("client_login") if isinstance(item, dict) else item
        if cand and EXPECTED_LOGIN_PART in str(cand).lower():
            login = str(cand).strip()
            if isinstance(item, dict) and item.get("token"):
                token = str(item["token"]).strip()
            break
    if not login:
        sys.exit("логин BJORN не найден")

    for camp in (SOURCE, CLONE):
        print(f"\n{'='*70}\nкампания {camp}: типы объявлений")
        res = call("ads", {
            "SelectionCriteria": {"CampaignIds": [camp]},
            "FieldNames": ["Id", "AdGroupId", "Type", "Subtype", "State", "Status"],
            "Page": {"Limit": 1000}}, login, token)
        if res.get("error"):
            print(f"  ошибка: {res['error'].get('error_string')} "
                  f"{res['error'].get('error_detail')}")
            continue
        ads = (res.get("result") or {}).get("Ads", [])
        acc = {}
        for a in ads:
            key = (a.get("Type"), a.get("Subtype"), a.get("Status"))
            acc[key] = acc.get(key, 0) + 1
        print(f"  всего объявлений {len(ads)}")
        for k, v in sorted(acc.items(), key=lambda x: str(x[0])):
            print(f"    Type={k[0]} Subtype={k[1]} Status={k[2]} -> {v}")

    print(f"\n{'='*70}\nполные поля одного объявления каждого блока (кампания {SOURCE})")
    for block, fields in BLOCKS.items():
        res = call("ads", {
            "SelectionCriteria": {"CampaignIds": [SOURCE]},
            "FieldNames": ["Id", "AdGroupId", "Type", "Subtype"],
            block: fields,
            "Page": {"Limit": 3}}, login, token)
        print(f"\n--- {block}")
        if res.get("error"):
            print(f"  ошибка: {res['error'].get('error_string')} | "
                  f"{res['error'].get('error_detail')}")
            continue
        for a in (res.get("result") or {}).get("Ads", []):
            print(json.dumps(a, ensure_ascii=False, indent=2)[:1800])
            break


if __name__ == "__main__":
    main()
