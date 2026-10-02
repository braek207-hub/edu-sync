"""Поля товарного объявления SHOPPING_AD и требования к группе.

В образце 714000003 на каждую группу два объявления: ТГО и товарное. Клон получил только
ТГО. Имена блока полей не угадываем: посылаем в блок заведомо неверное значение — API в
ошибке перечисляет все допустимые. Потом читаем товарное объявление целиком и сравниваем
группы образца и клона: чего группе клона не хватает, чтобы принять товарное объявление.
Только чтение.
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

    print("### какие блоки полей существуют для товарного объявления")
    for block in ("ShoppingAdFieldNames", "ShoppingAdBuilderAdFieldNames",
                  "UnifiedAdFieldNames"):
        res = call("ads", {
            "SelectionCriteria": {"CampaignIds": [SOURCE], "Types": ["SHOPPING_AD"]},
            "FieldNames": ["Id"], block: ["ZZ_UNKNOWN"], "Page": {"Limit": 1}}, login, token)
        err = res.get("error") or {}
        print(f"\n--- {block}")
        print(f"  {err.get('error_detail') or err.get('error_string') or 'блок принят'}")

    print("\n### товарное объявление целиком (подставим найденные поля вторым прогоном)")
    res = call("ads", {
        "SelectionCriteria": {"CampaignIds": [SOURCE], "Types": ["SHOPPING_AD"]},
        "FieldNames": ["Id", "AdGroupId", "CampaignId", "Type", "Subtype", "State",
                       "Status", "StatusClarification"],
        "Page": {"Limit": 2}}, login, token)
    for a in (res.get("result") or {}).get("Ads", []):
        print(json.dumps(a, ensure_ascii=False, indent=2))

    print("\n### группы: образец против клона")
    for camp in (SOURCE, CLONE):
        res = call("adgroups", {
            "SelectionCriteria": {"CampaignIds": [camp]},
            "FieldNames": ["Id", "Name", "Type", "Subtype", "Status", "RegionIds",
                           "TrackingParams"],
            "Page": {"Limit": 1000}}, login, token)
        rows = (res.get("result") or {}).get("AdGroups", [])
        acc = {}
        for g in rows:
            key = (g.get("Type"), g.get("Subtype"))
            acc[key] = acc.get(key, 0) + 1
        print(f"\n  кампания {camp}: групп {len(rows)}")
        for k, v in acc.items():
            print(f"    Type={k[0]} Subtype={k[1]} -> {v}")
        if rows:
            print(f"    пример: {json.dumps(rows[0], ensure_ascii=False)[:300]}")

    print("\n### что за блоки полей есть у групп (для товарных нужен свой)")
    for block in ("DynamicTextAdGroupFieldNames", "SmartAdGroupFieldNames",
                  "ShoppingAdGroupFieldNames"):
        res = call("adgroups", {
            "SelectionCriteria": {"CampaignIds": [SOURCE]},
            "FieldNames": ["Id"], block: ["ZZ_UNKNOWN"], "Page": {"Limit": 1}}, login, token)
        err = res.get("error") or {}
        print(f"\n--- {block}")
        print(f"  {err.get('error_detail') or err.get('error_string') or 'блок принят'}")


if __name__ == "__main__":
    main()
