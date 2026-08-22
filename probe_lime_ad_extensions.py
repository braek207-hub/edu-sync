# Проба Ф6: что реально приходит у объявлений ЕПК LIME сверх Title/Text и какие
# справочники дают им человеческие имена. Read-only (только get). Печатает сырые записи.
import json
import os

import requests

TOKEN = os.environ["DIRECT_TOKEN"]
LOGIN = os.environ["DIRECT_CLIENT_LOGIN"]
V501 = "https://api.direct.yandex.com/json/v501"
V5 = "https://api.direct.yandex.com/json/v5"


def call(base: str, service: str, params: dict, method: str = "get") -> dict:
    resp = requests.post(
        f"{base}/{service}",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Client-Login": LOGIN,
            "Accept-Language": "ru",
            "Content-Type": "application/json; charset=utf-8",
        },
        data=json.dumps({"method": method, "params": params}, ensure_ascii=False).encode("utf-8"),
        timeout=120,
    )
    return resp.json()


def err(body: dict) -> str | None:
    e = body.get("error")
    if not e:
        return None
    return f"ERROR {e.get('error_code')} {e.get('error_string')}: {e.get('error_detail')}"


print("=== 1. Допустимые FieldNames сервиса ads (bogus-трюк) ===")
for key in ("FieldNames", "TextAdFieldNames", "TextImageAdFieldNames"):
    body = call(V501, "ads", {"SelectionCriteria": {}, key: ["Bogus"], "Page": {"Limit": 1}})
    print(f"  {key}: {err(body)}")

print("\n=== 2. Активные ЕПК: берём 2 кампании с объявлениями ===")
camps = call(
    V501,
    "campaigns",
    {
        "SelectionCriteria": {"Types": ["UNIFIED_CAMPAIGN"], "States": ["ON"]},
        "FieldNames": ["Id", "Name"],
        "Page": {"Limit": 5},
    },
)
if err(camps):
    print("  ", err(camps))
    raise SystemExit(1)
camp_ids = [c["Id"] for c in camps["result"]["Campaigns"]]
print("  campaigns:", [(c["Id"], c["Name"]) for c in camps["result"]["Campaigns"]])

print("\n=== 3. ads.get с РАСШИРЕННЫМИ FieldNames ===")
ads_params = {
    "SelectionCriteria": {"CampaignIds": camp_ids},
    "FieldNames": ["Id", "AdGroupId", "CampaignId", "Type", "Subtype", "Status", "State"],
    "TextAdFieldNames": [
        "Title",
        "Title2",
        "Text",
        "Href",
        "Mobile",
        "DisplayDomain",
        "DisplayUrlPath",
        "SitelinkSetId",
        "AdImageHash",
        "VCardId",
        "AdExtensionIds",
        "TurboPageId",
        "PriceExtension",
        "BusinessId",
        "PreferVCardOverBusiness",
    ],
    "TextImageAdFieldNames": ["AdImageHash", "Href", "TurboPageId"],
    "Page": {"Limit": 20},
}
ads = call(V501, "ads", ads_params)
if err(ads):
    print("  ", err(ads))
else:
    items = ads["result"].get("Ads", [])
    print(f"  ads={len(items)}")
    seen_types: dict[str, int] = {}
    for a in items:
        t = a.get("Type", "?")
        seen_types[t] = seen_types.get(t, 0) + 1
        if seen_types[t] <= 2:
            print("   ", json.dumps(a, ensure_ascii=False)[:700])
    print("  типы:", seen_types)

print("\n=== 4. Справочники расширений ===")
sl = call(V501, "sitelinks", {"SelectionCriteria": {}, "FieldNames": ["Id", "Sitelinks"], "Page": {"Limit": 3}})
print("  sitelinks:", err(sl) or json.dumps(sl["result"], ensure_ascii=False)[:700])

ax = call(
    V501,
    "adextensions",
    {"SelectionCriteria": {}, "FieldNames": ["Id", "Type", "State", "Status"], "CalloutFieldNames": ["CalloutText"], "Page": {"Limit": 5}},
)
print("  adextensions:", err(ax) or json.dumps(ax["result"], ensure_ascii=False)[:700])

vc = call(V501, "vcards", {"SelectionCriteria": {}, "FieldNames": ["Id", "CompanyName", "Phone"], "Page": {"Limit": 2}})
print("  vcards:", err(vc) or json.dumps(vc["result"], ensure_ascii=False)[:400])

print("\n=== 5. Цели по счётчикам кампаний (goals.get) ===")
uc = call(
    V501,
    "campaigns",
    {
        "SelectionCriteria": {"Ids": camp_ids[:2]},
        "FieldNames": ["Id", "Name"],
        "UnifiedCampaignFieldNames": ["CounterIds", "PriorityGoals", "Settings", "BiddingStrategy"],
    },
)
if err(uc):
    print("  ", err(uc))
else:
    counters: set[int] = set()
    for c in uc["result"]["Campaigns"]:
        u = c.get("UnifiedCampaign") or {}
        ids = (u.get("CounterIds") or {}).get("Items") or []
        counters.update(ids)
        print("   camp", c["Id"], "counters", ids)
        print("     PriorityGoals:", json.dumps(u.get("PriorityGoals"), ensure_ascii=False)[:300])
        print("     Settings:", json.dumps(u.get("Settings"), ensure_ascii=False)[:400])
        print("     BiddingStrategy:", json.dumps(u.get("BiddingStrategy"), ensure_ascii=False)[:500])
    if counters:
        for base, name in ((V501, "v501"), (V5, "v5")):
            g = call(base, "goals", {"SelectionCriteria": {"CounterIds": sorted(counters)[:10]}, "FieldNames": ["Id", "Name", "Type"]})
            print(f"  goals [{name}]:", err(g) or json.dumps(g["result"], ensure_ascii=False)[:600])
