# Проба Ф6 (раунд 4): отдаёт ли API человеческое имя и превью картинки по AdImageHash.
# Нужно, чтобы в конструкторе запуска показать креатив объявления, а не голый хеш.
# Read-only (только get). Формы enum вскрываются bogus-трюком, не догадкой.
import json
import os

import requests

TOKEN = os.environ["DIRECT_TOKEN"]
LOGIN = os.environ["DIRECT_CLIENT_LOGIN"]
V501 = "https://api.direct.yandex.com/json/v501"


def call(service: str, params: dict, method: str = "get") -> dict:
    resp = requests.post(
        f"{V501}/{service}",
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


print("=== 1. Допустимые FieldNames сервиса adimages (bogus-трюк) ===")
print("  ", err(call("adimages", {"SelectionCriteria": {}, "FieldNames": ["Bogus"], "Page": {"Limit": 1}})))

print("\n=== 2. Хеши картинок реальных объявлений ЕПК ===")
camps = call(
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
ads = call(
    "ads",
    {
        "SelectionCriteria": {"CampaignIds": camp_ids},
        "FieldNames": ["Id", "Type"],
        "TextAdFieldNames": ["AdImageHash"],
        "TextImageAdFieldNames": ["AdImageHash"],
        "Page": {"Limit": 60},
    },
)
hashes: list[str] = []
if err(ads):
    print("  ", err(ads))
else:
    for a in ads["result"].get("Ads", []):
        h = (a.get("TextAd") or {}).get("AdImageHash") or (a.get("TextImageAd") or {}).get("AdImageHash")
        if h and h not in hashes:
            hashes.append(h)
    print(f"  уникальных хешей: {len(hashes)}; первые: {hashes[:5]}")

print("\n=== 3. adimages.get по этим хешам, полный набор полей ===")
if hashes:
    for fields in (
        ["AdImageHash", "Name", "Type", "Subtype", "PreviewUrl", "OriginalUrl", "AssociatedAdsCount"],
        ["AdImageHash", "Name", "Type", "Subtype"],
    ):
        res = call(
            "adimages",
            {"SelectionCriteria": {"AdImageHashes": hashes[:10]}, "FieldNames": fields},
        )
        e = err(res)
        print(f"  FieldNames={fields}: {e or 'OK'}")
        if not e:
            for item in res["result"].get("AdImages", [])[:5]:
                print("   ", json.dumps(item, ensure_ascii=False)[:400])
            break
else:
    print("  хешей нет — пропуск")
