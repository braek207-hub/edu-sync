"""Что сейчас в ТГО дубля 715011487 и образца 714000003: заголовки, тексты, расширения.

PriceExtension у TEXT_AD в API нет — допустимые поля перечислила сама ошибка Директа.
Смотрим, где в образце живут цены: AdExtensions, Carousel, ButtonExtension.
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
    "Title", "Title2", "Text", "Href", "DisplayUrlPath", "AdImageHash",
    "SitelinkSetId", "AdExtensions", "VideoExtension", "TurboPageId", "BusinessId", "VCardId", "TrackingParams",
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
        print(f"\n=== кампания {camp}: ТГО {len(ads)}")
        keys: dict = {}
        for a in ads:
            for k, v in (a.get("TextAd") or {}).items():
                if v not in (None, [], {}):
                    keys[k] = keys.get(k, 0) + 1
        print(f"заполненные поля: {json.dumps(keys, ensure_ascii=False)}")
        for a in ads[:2]:
            print(json.dumps(a, ensure_ascii=False))
        titles = {}
        for a in ads:
            ta = a.get("TextAd") or {}
            titles[(ta.get("Title", ""), ta.get("Title2") or "")] = \
                titles.get((ta.get("Title", ""), ta.get("Title2") or ""), 0) + 1
        print(f"уникальных пар заголовков: {len(titles)}")
        for (t1, t2), n in sorted(titles.items(), key=lambda x: -x[1])[:6]:
            print(f"  {n}x «{t1}» | «{t2}»")
        texts = {(a.get("TextAd") or {}).get("Text", "") for a in ads}
        print(f"уникальных текстов: {len(texts)}")
        for t in list(texts)[:4]:
            print(f"  «{t}»")

    ext = call("adextensions", {
        "SelectionCriteria": {}, "FieldNames": ["Id", "Type", "Associated", "State"],
        "CalloutFieldNames": ["CalloutText"], "Page": {"Limit": 100}}, login, token)
    if ext.get("error"):
        print(f"\nadextensions: ОШИБКА {ext['error'].get('error_detail')}")
    else:
        rows = (ext.get("result") or {}).get("AdExtensions", [])
        print(f"\nрасширений в кабинете {len(rows)}")
        for r in rows[:10]:
            print(f"  {json.dumps(r, ensure_ascii=False)}")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
