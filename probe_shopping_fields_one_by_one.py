"""Какие поля ShoppingAdFieldNames принимает Директ — проверяем по одному.

Сообщение с перечислением приходит обрезанным, поэтому вместо чтения ошибки шлём каждое
поле отдельным запросом: принято или нет. Только чтение.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.direct.yandex.com/json/v5/"
SOURCE = 714000003
CANDIDATES = ["SitelinkSetId", "SitelinksModeration", "AdExtensions", "BusinessId",
              "TrackingPhoneId", "FeedId", "FeedFilterConditions", "FeedProcessingStatus",
              "TitleSources", "TextSources", "DefaultTexts", "BodySources",
              "DefaultBodies", "GenerationScopes", "ListingFeedFilterConditions",
              "ListingTitleSources", "ListingTextSources"]
EXPECTED_LOGIN_PART = "bjorn"


def call(params: dict, login: str, token: str) -> dict:
    body = json.dumps({"method": "get", "params": params}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API + "ads", data=body, headers={
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

    good = []
    for f in CANDIDATES:
        res = call({"SelectionCriteria": {"CampaignIds": [SOURCE], "Types": ["SHOPPING_AD"]},
                    "FieldNames": ["Id"], "ShoppingAdFieldNames": [f],
                    "Page": {"Limit": 1}}, login, token)
        if res.get("error"):
            print(f"  {f:<32} ОТКАЗ")
        else:
            good.append(f)
            print(f"  {f:<32} ок")

    print(f"\nпринимаемые поля ({len(good)}): {good}")
    res = call({"SelectionCriteria": {"CampaignIds": [SOURCE], "Types": ["SHOPPING_AD"]},
                "FieldNames": ["Id", "AdGroupId"], "ShoppingAdFieldNames": good,
                "Page": {"Limit": 1}}, login, token)
    for a in (res.get("result") or {}).get("Ads", []):
        print("\nтоварное объявление целиком:")
        print(json.dumps(a, ensure_ascii=False, indent=2)[:2500])


if __name__ == "__main__":
    main()
