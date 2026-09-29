"""Полный слепок 714000003 под клон: поля с исправленными перечислениями.

Прошлый прогон споткнулся на трёх enum — API сам перечислил валидные значения, здесь
они подставлены. Дополнительно: корректировки ставок (включая погодные — нужно понять,
можно ли их создать заново) и минус-площадки целиком.

Read-only.
"""

from __future__ import annotations

import json
import os

import requests

API = "https://api.direct.yandex.com/json/v5/"
CAMPAIGN = 714000003
SAMPLE_GROUP = 5796196259


def call(service: str, params: dict, login: str, token: str, method: str = "get") -> dict:
    body = json.dumps({"method": method, "params": params}, ensure_ascii=False).encode("utf-8")
    resp = requests.post(API + service, data=body, headers={
        "Authorization": f"Bearer {token}",
        "Client-Login": login,
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
    }, timeout=120)
    try:
        return resp.json()
    except Exception:
        return {"error": {"error_string": f"HTTP {resp.status_code}", "error_detail": resp.text[:300]}}


def clients() -> list[tuple[str, str]]:
    default_token = os.environ.get("DIRECT_TOKEN", "").strip()
    raw = os.environ.get("DIRECT_CLIENTS_JSON", "").strip()
    out: list[tuple[str, str]] = []
    if raw:
        for item in json.loads(raw):
            if isinstance(item, dict):
                login = str(item.get("login") or item.get("client_login") or "").strip()
                token = str(item.get("token") or "").strip() or default_token
                if login:
                    out.append((login, token))
            elif isinstance(item, str):
                out.append((item.strip(), default_token))
    return out


def dump(login: str, token: str) -> None:
    print("### НАСТРОЙКИ КАМПАНИИ (Settings целиком) + счётчики + разметка")
    r = call("campaigns", {
        "SelectionCriteria": {"Ids": [CAMPAIGN]},
        "FieldNames": ["Id", "Name", "ExcludedSites", "NegativeKeywords", "TimeTargeting"],
        "TextCampaignFieldNames": ["Settings", "CounterIds", "PriorityGoals",
                                   "AttributionModel", "TrackingParams", "RelevantKeywords"],
        "Page": {"Limit": 1},
    }, login, token)
    c = (r.get("result", {}).get("Campaigns") or [{}])[0]
    tc = c.get("TextCampaign", {})
    print(f"  Settings: {json.dumps(tc.get('Settings'), ensure_ascii=False)}")
    print(f"  CounterIds: {json.dumps(tc.get('CounterIds'), ensure_ascii=False)}")
    print(f"  PriorityGoals: {json.dumps(tc.get('PriorityGoals'), ensure_ascii=False)[:400]}")
    print(f"  AttributionModel: {tc.get('AttributionModel')}")
    print(f"  TrackingParams: {tc.get('TrackingParams')}")
    print(f"  RelevantKeywords: {json.dumps(tc.get('RelevantKeywords'), ensure_ascii=False)}")
    ex = (c.get("ExcludedSites") or {}).get("Items") or []
    print(f"  ExcludedSites: {len(ex)} шт, первые 5: {ex[:5]}")
    print(f"  NegativeKeywords: {json.dumps(c.get('NegativeKeywords'), ensure_ascii=False)[:300]}")

    print(f"\n### ГРУППА {SAMPLE_GROUP}")
    g = call("adgroups", {
        "SelectionCriteria": {"Ids": [SAMPLE_GROUP]},
        "FieldNames": ["Id", "Name", "RegionIds", "NegativeKeywords", "TrackingParams",
                       "Type", "Subtype", "Status"],
        "TextAdGroupFeedParamsFieldNames": ["FeedId", "FeedCategoryIds"],
        "Page": {"Limit": 1},
    }, login, token)
    print(f"  {json.dumps(g.get('result') or g, ensure_ascii=False)[:1200]}")

    print(f"\n### ОБЪЯВЛЕНИЯ ГРУППЫ {SAMPLE_GROUP} — слепок для копирования")
    a = call("ads", {
        "SelectionCriteria": {"AdGroupIds": [SAMPLE_GROUP]},
        "FieldNames": ["Id", "AdGroupId", "Type", "Subtype", "State", "Status", "AdCategories"],
        "TextAdFieldNames": ["Title", "Title2", "Text", "Href", "Mobile", "DisplayDomain",
                             "DisplayUrlPath", "AdImageHash", "LogoExtensionHash",
                             "SitelinkSetId", "VCardId", "AdExtensions", "VideoExtension",
                             "TurboPageId", "BusinessId", "PreferVCardOverBusiness",
                             "ButtonExtension", "TrackingParams", "Carousel"],
        "Page": {"Limit": 10},
    }, login, token)
    print(json.dumps(a.get("result") or a, ensure_ascii=False, indent=1)[:3000])

    print("\n### ФИДЫ")
    f = call("feeds", {
        "FieldNames": ["Id", "Name", "BusinessType", "SourceType", "Status",
                       "NumberOfItems", "CampaignIds"],
    }, login, token)
    print(f"  {json.dumps(f.get('result') or f, ensure_ascii=False)[:800]}")

    print("\n### КОРРЕКТИРОВКИ: перечисление валидных полей (намеренно неверный enum)")
    b = call("bidmodifiers", {
        "SelectionCriteria": {"CampaignIds": [CAMPAIGN], "Levels": ["CAMPAIGN", "AD_GROUP"]},
        "FieldNames": ["Id", "CampaignId", "AdGroupId", "Level", "Type"],
        "WeatherAdjustmentFieldNames": ["__probe__"],
        "Page": {"Limit": 5},
    }, login, token)
    print(f"  {json.dumps(b.get('error') or b.get('result'), ensure_ascii=False)[:900]}")

    print("\n### КОРРЕКТИРОВКИ ГРУППЫ: демография и погода")
    b2 = call("bidmodifiers", {
        "SelectionCriteria": {"AdGroupIds": [SAMPLE_GROUP],
                              "Levels": ["AD_GROUP"]},
        "FieldNames": ["Id", "CampaignId", "AdGroupId", "Level", "Type"],
        "DemographicsAdjustmentFieldNames": ["Gender", "Age", "BidModifier"],
        "Page": {"Limit": 20},
    }, login, token)
    print(f"  {json.dumps(b2.get('error') or b2.get('result'), ensure_ascii=False)[:1200]}")


def main() -> None:
    for login, token in clients():
        if token:
            print(f"аккаунт {login}\n")
            dump(login, token)
            return


if __name__ == "__main__":
    main()
