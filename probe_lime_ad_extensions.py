# Проба Ф6 (раунд 2): что реально приходит у объявлений ЕПК LIME сверх Title/Text
# и какие справочники дают им человеческие имена. Read-only (только get).
# Формы полей взяты из ответа API раунда 1 (bogus-трюк), не из догадок.
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
    try:
        return resp.json()
    except Exception:
        return {"error": {"error_code": resp.status_code, "error_string": "не JSON", "error_detail": resp.text[:200]}}


def err(body: dict) -> str | None:
    e = body.get("error")
    if not e:
        return None
    return f"ERROR {e.get('error_code')} {e.get('error_string')}: {e.get('error_detail')}"


print("=== 1. Активные ЕПК ===")
camps = call(
    V501,
    "campaigns",
    {
        "SelectionCriteria": {"Types": ["UNIFIED_CAMPAIGN"], "States": ["ON"]},
        "FieldNames": ["Id", "Name"],
        "Page": {"Limit": 6},
    },
)
if err(camps):
    print("  ", err(camps))
    raise SystemExit(1)
camp_ids = [c["Id"] for c in camps["result"]["Campaigns"]]
print("  ", [(c["Id"], c["Name"]) for c in camps["result"]["Campaigns"]])

print("\n=== 2. ads.get, ПОЛНЫЙ набор полей TextAd (список выдал сам API) ===")
ads = call(
    V501,
    "ads",
    {
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
            "AdExtensions",
            "TurboPageId",
            "BusinessId",
            "PreferVCardOverBusiness",
            "ButtonExtension",
            "TrackingParams",
            "Carousel",
            "LogoExtensionHash",
        ],
        "TextImageAdFieldNames": ["AdImageHash", "Href", "TurboPageId", "TrackingParams"],
        "Page": {"Limit": 40},
    },
)
sitelink_ids: set[int] = set()
vcard_ids: set[int] = set()
ext_ids: set[int] = set()
if err(ads):
    print("  ", err(ads))
else:
    items = ads["result"].get("Ads", [])
    print(f"  ads={len(items)}")
    per_type: dict[str, int] = {}
    for a in items:
        t = a.get("Type", "?")
        per_type[t] = per_type.get(t, 0) + 1
        if per_type[t] <= 2:
            print("   ", json.dumps(a, ensure_ascii=False)[:900])
        ta = a.get("TextAd") or {}
        if ta.get("SitelinkSetId"):
            sitelink_ids.add(ta["SitelinkSetId"])
        if ta.get("VCardId"):
            vcard_ids.add(ta["VCardId"])
        for ext in ta.get("AdExtensions") or []:
            if ext.get("AdExtensionId"):
                ext_ids.add(ext["AdExtensionId"])
    print("  типы:", per_type)
    print("  найдено SitelinkSetId:", sorted(sitelink_ids)[:5], "VCardId:", sorted(vcard_ids)[:5], "AdExtensionId:", sorted(ext_ids)[:5])

print("\n=== 3. Справочники по найденным Ids ===")
if sitelink_ids:
    sl = call(V501, "sitelinks", {"SelectionCriteria": {"Ids": sorted(sitelink_ids)[:3]}, "FieldNames": ["Id", "Sitelinks"]})
    print("  sitelinks:", err(sl) or json.dumps(sl["result"], ensure_ascii=False)[:900])
else:
    print("  sitelinks: у объявлений нет SitelinkSetId")

if vcard_ids:
    vc = call(V501, "vcards", {"SelectionCriteria": {"Ids": sorted(vcard_ids)[:2]}, "FieldNames": ["Id", "CompanyName", "Phone", "Country", "City"]})
    print("  vcards:", err(vc) or json.dumps(vc["result"], ensure_ascii=False)[:500])
else:
    print("  vcards: у объявлений нет VCardId")

ax = call(
    V501,
    "adextensions",
    {"SelectionCriteria": {}, "FieldNames": ["Id", "Type", "State", "Status"], "CalloutFieldNames": ["CalloutText"], "Page": {"Limit": 20}},
)
print("  adextensions (все аккаунта):", err(ax) or json.dumps(ax["result"], ensure_ascii=False)[:900])

print("\n=== 4. Цели: goals.get только на v5 (на v501 сервиса нет) ===")
uc = call(
    V501,
    "campaigns",
    {
        "SelectionCriteria": {"Ids": camp_ids[:3]},
        "FieldNames": ["Id"],
        "UnifiedCampaignFieldNames": ["CounterIds"],
    },
)
counters: set[int] = set()
if not err(uc):
    for c in uc["result"]["Campaigns"]:
        counters.update(((c.get("UnifiedCampaign") or {}).get("CounterIds") or {}).get("Items") or [])
print("  counters:", sorted(counters))
if counters:
    g = call(V5, "goals", {"SelectionCriteria": {"CounterIds": sorted(counters)[:10]}, "FieldNames": ["Id", "Name", "Type"]})
    if err(g):
        print("  goals v5:", err(g))
    else:
        goals = g["result"].get("Goals", [])
        print(f"  goals v5: {len(goals)} целей")
        for go in goals[:10]:
            print("   ", json.dumps(go, ensure_ascii=False))
        for want in (3023504302, 1900016999, 1900017000):
            hit = next((x for x in goals if x.get("Id") == want), None)
            print(f"   искомая {want}:", json.dumps(hit, ensure_ascii=False) if hit else "НЕТ в ответе")

print("\n=== 5. Формы записи: bogus по ads.add (какие поля принимает TextAd на запись) ===")
add_probe = call(
    V501,
    "ads",
    {"Ads": [{"AdGroupId": 1, "TextAd": {"Bogus": 1, "Title": "t", "Text": "x", "Href": "https://limestore.com"}}]},
    method="add",
)
print("  ads.add bogus:", json.dumps(add_probe.get("error", add_probe), ensure_ascii=False)[:900])
