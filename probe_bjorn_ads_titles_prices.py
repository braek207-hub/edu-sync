"""Что сейчас в ТГО дубля 715011487 и образца 714000003: заголовки, тексты, цены.

Нужно перед правкой: откуда брать «старую» цену (вдруг PriceExtension уже заполнен
в образце) и в каком виде Директ её отдаёт — целое, микро, строка.
Только чтение.
"""

from __future__ import annotations

import json
import os
import sys

import requests

API = "https://api.direct.yandex.com/json/v5/"
CAMPAIGNS = [715011487, 714000003]
EXPECTED_LOGIN_PART = "bjorn"

TEXT_FIELDS = [
    "Title", "Title2", "Text", "Href", "DisplayUrlPath",
    "AdImageHash", "SitelinkSetId", "AdExtensionIds", "PriceExtension",
    "VideoExtension", "TurboPageId", "BusinessId", "VCardId",
]


def call(service: str, params: dict, login: str, token: str, method: str = "get") -> dict:
    body = json.dumps({"method": method, "params": params}, ensure_ascii=False).encode("utf-8")
    resp = requests.post(API + service, data=body, headers={
        "Authorization": f"Bearer {token}", "Client-Login": login,
        "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8"},
        timeout=180)
    try:
        return resp.json()
    except Exception:
        return {"error": {"error_string": f"HTTP {resp.status_code}",
                          "error_detail": resp.text[:300]}}


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
    print(f"кабинет {login}")

    for camp in CAMPAIGNS:
        res = call("ads", {
            "SelectionCriteria": {"CampaignIds": [camp], "Types": ["TEXT_AD"]},
            "FieldNames": ["Id", "AdGroupId", "State", "Status"],
            "TextAdFieldNames": TEXT_FIELDS, "Page": {"Limit": 1000}}, login, token)
        if res.get("error"):
            print(f"\n{camp}: ОШИБКА {res['error'].get('error_string')} | "
                  f"{res['error'].get('error_detail')}")
            continue
        ads = (res.get("result") or {}).get("Ads", [])
        with_price = sum(1 for a in ads if (a.get("TextAd") or {}).get("PriceExtension"))
        print(f"\n=== кампания {camp}: ТГО {len(ads)}, с блоком цены {with_price}")
        for a in ads[:3]:
            print(json.dumps(a, ensure_ascii=False))
        if len(ads) > 3:
            print(f"... ещё {len(ads) - 3}")
        # все уникальные заголовки — чтобы видеть, что правим
        titles = {}
        for a in ads:
            t = (a.get("TextAd") or {}).get("Title", "")
            titles[t] = titles.get(t, 0) + 1
        print(f"уникальных Title: {len(titles)}")
        for t, n in sorted(titles.items(), key=lambda x: -x[1])[:8]:
            print(f"  {n}x «{t}»")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
