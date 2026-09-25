"""Что такое кампания 714787146 в BJORN: тип, группы, объявления, текущие креативы.

Нужно до замены креативов: понять, поддерживает ли API запись в этот тип кампании,
или креативы живут в конструкторе и меняются только браузером.

Read-only.
"""

from __future__ import annotations

import json
import os

import requests

API = "https://api.direct.yandex.com/json/v5/"
CAMPAIGN_ID = 714787146


def call(service: str, params: dict, login: str, token: str, method: str = "get") -> dict:
    body = json.dumps({"method": method, "params": params}, ensure_ascii=False).encode("utf-8")
    resp = requests.post(
        API + service,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Client-Login": login,
            "Accept-Language": "ru",
            "Content-Type": "application/json; charset=utf-8",
        },
        timeout=120,
    )
    try:
        return resp.json()
    except Exception:
        return {"error": {"error_string": f"HTTP {resp.status_code}", "error_detail": resp.text[:300]}}


def err(body: dict) -> str | None:
    e = body.get("error")
    return None if not e else f"{e.get('error_string')} | {e.get('error_detail')}"[:400]


def clients() -> list[tuple[str, str]]:
    default_token = os.environ.get("DIRECT_TOKEN", "").strip()
    raw = os.environ.get("DIRECT_CLIENTS_JSON", "").strip()
    out: list[tuple[str, str]] = []
    if raw:
        data = json.loads(raw)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, str):
                    out.append((item.strip(), default_token))
                elif isinstance(item, dict):
                    login = str(item.get("login") or item.get("client_login") or "").strip()
                    token = str(item.get("token") or "").strip() or default_token
                    if login:
                        out.append((login, token))
    return out


def probe(login: str, token: str) -> bool:
    camps = call("campaigns", {
        "SelectionCriteria": {"Ids": [CAMPAIGN_ID]},
        "FieldNames": ["Id", "Name", "Type", "State", "Status", "StatusClarification",
                       "StartDate", "DailyBudget", "NegativeKeywords"],
        "TextCampaignFieldNames": ["BiddingStrategy", "Settings"],
        "CpmBannerCampaignFieldNames": ["BiddingStrategy", "Settings"],
        "Page": {"Limit": 10},
    }, login, token)
    e = err(camps)
    if e:
        print(f"[{login}] campaigns.get: {e}")
        return False
    cl = camps.get("result", {}).get("Campaigns", [])
    if not cl:
        print(f"[{login}] кампании {CAMPAIGN_ID} нет")
        return False

    c = cl[0]
    print("=" * 78)
    print(f"АККАУНТ {login} · КАМПАНИЯ {CAMPAIGN_ID}")
    print("=" * 78)
    print(json.dumps(c, ensure_ascii=False, indent=2)[:3000])

    groups = call("adgroups", {
        "SelectionCriteria": {"CampaignIds": [CAMPAIGN_ID]},
        "FieldNames": ["Id", "Name", "CampaignId", "Type", "Subtype", "Status", "RegionIds"],
        "Page": {"Limit": 500},
    }, login, token)
    e = err(groups)
    gl = [] if e else groups.get("result", {}).get("AdGroups", [])
    print(f"\n--- Группы ({len(gl)}) {'ОШИБКА: ' + e if e else ''} ---")
    for g in gl:
        print(f"  {g['Id']} | {g.get('Type')}/{g.get('Subtype')} | {g.get('Status')} | {g.get('Name')}")

    ads = call("ads", {
        "SelectionCriteria": {"CampaignIds": [CAMPAIGN_ID]},
        "FieldNames": ["Id", "AdGroupId", "Type", "Subtype", "State", "Status",
                       "StatusClarification"],
        "TextAdFieldNames": ["Title", "AdImageHash", "VideoExtension", "Href"],
        "TextImageAdFieldNames": ["AdImageHash", "Href"],
        "TextAdBuilderAdFieldNames": ["Creative", "Href"],
        "CpmBannerAdBuilderAdFieldNames": ["Creative", "Href", "TrackingPixels"],
        "Page": {"Limit": 500},
    }, login, token)
    e = err(ads)
    if e:
        print(f"\nads.get ОШИБКА (полный набор блоков): {e}")
        ads = call("ads", {
            "SelectionCriteria": {"CampaignIds": [CAMPAIGN_ID]},
            "FieldNames": ["Id", "AdGroupId", "Type", "Subtype", "State", "Status",
                           "StatusClarification"],
            "Page": {"Limit": 500},
        }, login, token)
        e = err(ads)
    al = [] if e else ads.get("result", {}).get("Ads", [])
    print(f"\n--- Объявления ({len(al)}) {'ОШИБКА: ' + e if e else ''} ---")
    for a in al:
        print(json.dumps(a, ensure_ascii=False)[:600])

    # Какие креативы и картинки вообще числятся у аккаунта — чтобы понять, куда грузить
    cre = call("creatives", {
        "SelectionCriteria": {},
        "FieldNames": ["Id", "Type", "Name", "Width", "Height", "PreviewUrl"],
        "Page": {"Limit": 500},
    }, login, token)
    e = err(cre)
    if e:
        print(f"\ncreatives.get: {e}")
    else:
        cr = cre.get("result", {}).get("Creatives", [])
        used = {a.get("CpmBannerAdBuilderAd", {}).get("Creative", {}).get("CreativeId")
                for a in al if isinstance(a.get("CpmBannerAdBuilderAd"), dict)}
        used |= {a.get("TextAdBuilderAd", {}).get("Creative", {}).get("CreativeId")
                 for a in al if isinstance(a.get("TextAdBuilderAd"), dict)}
        used.discard(None)
        print(f"\n--- Креативы, используемые этой кампанией: {used or 'нет'} ---")
        for c2 in cr:
            if c2["Id"] in used:
                print(f"  {json.dumps(c2, ensure_ascii=False)[:400]}")

    imgs = call("adimages", {
        "SelectionCriteria": {},
        "FieldNames": ["AdImageHash", "Name", "Type", "Subtype", "Associated"],
        "Page": {"Limit": 100},
    }, login, token)
    e = err(imgs)
    if e:
        print(f"\nadimages.get: {e}")
    else:
        il = imgs.get("result", {}).get("AdImages", [])
        print(f"\n--- Картинки в аккаунте: {len(il)} (первые 10) ---")
        for i in il[:10]:
            print(f"  {i.get('AdImageHash')} | {i.get('Type')}/{i.get('Subtype')} | "
                  f"assoc={i.get('Associated')} | {i.get('Name')}")
    return True


def main() -> None:
    for login, token in clients():
        if not token:
            continue
        try:
            if probe(login, token):
                return
        except Exception as exc:  # noqa: BLE001
            print(f"{login}: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
