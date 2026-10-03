"""Сколько заголовков и текстов осталось у ТГО 715011487 против образца 714000003.

Память direct-api-epk-write-traps: ads.update v5 урезает комбинаторное объявление до
одного заголовка и одного текста, отвечая ровно warning 10252 — его мы и получили.
Проверяем через v501 (ResponsiveAd.Titles/Texts) и сравниваем с образцом, который
правками не трогали. Только чтение.
"""

from __future__ import annotations

import json
import os
import sys

import requests

CAMPAIGNS = [715011487, 714000003]
EXPECTED_LOGIN_PART = "bjorn"


def call(version: str, service: str, params: dict, login: str, token: str,
         method: str = "get") -> dict:
    url = f"https://api.direct.yandex.com/json/{version}/{service}"
    body = json.dumps({"method": method, "params": params}, ensure_ascii=False).encode("utf-8")
    resp = requests.post(url, data=body, headers={
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
        for fields in (["Titles", "Texts", "Href"], ["Titles", "Texts"], ["Titles"]):
            res = call("v501", "ads", {
                "SelectionCriteria": {"CampaignIds": [camp]},
                "FieldNames": ["Id", "AdGroupId", "Type", "Subtype", "State", "Status"],
                "ResponsiveAdFieldNames": fields,
                "Page": {"Limit": 1000}}, login, token)
            if not res.get("error"):
                break
            print(f"\n{camp} | ResponsiveAdFieldNames={fields}: "
                  f"{res['error'].get('error_string')} | {res['error'].get('error_detail')}")
        if res.get("error"):
            continue
        ads = (res.get("result") or {}).get("Ads", [])
        print(f"\n=== кампания {camp}: объявлений {len(ads)}")
        acc: dict = {}
        for a in ads:
            ra = a.get("ResponsiveAd") or {}
            key = (a.get("Type"), a.get("Subtype"),
                   len(ra.get("Titles") or []), len(ra.get("Texts") or []))
            acc[key] = acc.get(key, 0) + 1
        for (t, sub, nt, ntx), n in sorted(acc.items(), key=lambda x: str(x[0])):
            print(f"  {t}/{sub}: заголовков {nt}, текстов {ntx} -> {n} объявлений")
        for a in ads[:2]:
            print(json.dumps(a, ensure_ascii=False)[:700])
    sys.stdout.flush()


if __name__ == "__main__":
    main()
