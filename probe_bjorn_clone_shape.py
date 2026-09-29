"""Что именно придётся пересоздать при клоне 714000003: поля кампании, групп и объявлений.

Нужно до написания сборщика: какие поля у ТГО (чтобы скопировать 1:1 с расширениями),
что вообще отдаёт API по товарному объявлению SHOPPING_AD и можно ли его создать заново,
какие настройки у кампании и какой фид у групп.

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
    print("### КАМПАНИЯ 714000003 — все настройки")
    r = call("campaigns", {
        "SelectionCriteria": {"Ids": [CAMPAIGN]},
        "FieldNames": ["Id", "Name", "StartDate", "EndDate", "TimeZone", "Currency",
                       "DailyBudget", "NegativeKeywords", "BlockedIps", "ExcludedSites",
                       "TimeTargeting", "Notification", "State", "Status", "Type",
                       "ClientInfo", "SourceId"],
        "TextCampaignFieldNames": ["BiddingStrategy", "Settings", "CounterIds",
                                   "RelevantKeywords", "PriorityGoals", "AttributionModel",
                                   "TrackingParams"],
        "Page": {"Limit": 1},
    }, login, token)
    c = (r.get("result", {}).get("Campaigns") or [{}])[0]
    for k, v in c.items():
        s = json.dumps(v, ensure_ascii=False)
        if k == "ExcludedSites":
            print(f"  {k}: {len(v.get('Items', [])) if isinstance(v, dict) else '?'} площадок")
            continue
        print(f"  {k}: {s[:700]}")
    if r.get("error"):
        print(f"  error: {r['error']}")

    print(f"\n### ГРУППА {SAMPLE_GROUP} — все поля, включая фид")
    g = call("adgroups", {
        "SelectionCriteria": {"Ids": [SAMPLE_GROUP]},
        "FieldNames": ["Id", "Name", "CampaignId", "RegionIds", "NegativeKeywords",
                       "TrackingParams", "Type", "Subtype", "Status"],
        "TextAdGroupFeedParamsFieldNames": ["FeedId", "FeedCategoryIds", "Source"],
        "Page": {"Limit": 1},
    }, login, token)
    print(f"  {json.dumps(g.get('result') or g, ensure_ascii=False)[:1500]}")

    print(f"\n### ОБЪЯВЛЕНИЯ ГРУППЫ {SAMPLE_GROUP} — полный слепок")
    a = call("ads", {
        "SelectionCriteria": {"AdGroupIds": [SAMPLE_GROUP]},
        "FieldNames": ["Id", "AdGroupId", "CampaignId", "Type", "Subtype", "State",
                       "Status", "AdCategories"],
        "TextAdFieldNames": ["Title", "Title2", "Text", "Href", "Mobile", "DisplayDomain",
                             "DisplayUrlPath", "AdImageHash", "SitelinkSetId", "VCardId",
                             "AdExtensionIds", "VideoExtension", "PriceExtension",
                             "TurboPageId", "BusinessId", "PreferVCardOverBusiness"],
        "Page": {"Limit": 10},
    }, login, token)
    print(f"  {json.dumps(a.get('result') or a, ensure_ascii=False, indent=1)[:2500]}")

    print("\n### ТОВАРНОЕ ОБЪЯВЛЕНИЕ: какие поля вообще существуют")
    for fields in (["Href", "AdImageHash", "SitelinkSetId"], ):
        b = call("ads", {
            "SelectionCriteria": {"AdGroupIds": [SAMPLE_GROUP], "Types": ["SHOPPING_AD"]},
            "FieldNames": ["Id", "Type", "Subtype", "State", "Status"],
            "Page": {"Limit": 5},
        }, login, token)
        print(f"  {json.dumps(b.get('result') or b, ensure_ascii=False)[:800]}")

    print("\n### ФИДЫ АККАУНТА")
    f = call("feeds", {
        "FieldNames": ["Id", "Name", "BusinessType", "SourceType", "UpdateStatus"],
    }, login, token)
    print(f"  {json.dumps(f.get('result') or f, ensure_ascii=False)[:900]}")


def main() -> None:
    for login, token in clients():
        if token:
            print(f"аккаунт {login}\n")
            dump(login, token)
            return


if __name__ == "__main__":
    main()
