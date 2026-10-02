"""Кому принадлежит BJORN_DIRECT_TOKEN и через кого он видит кабинет.

В интерфейсе approve.agency кабинет audit-bjornlarsen не открывается («Доступ ограничен»),
а API с этим токеном в него пишет. Значит токен принадлежит другому логину — его и надо
знать, чтобы зайти в интерфейс и клонировать кампанию. Только чтение.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

API = "https://api.direct.yandex.com/json/v5/"
EXPECTED_LOGIN_PART = "bjorn"


def call(service: str, params: dict, token: str, login: str = "") -> dict:
    headers = {"Authorization": "Bearer " + token, "Accept-Language": "ru",
               "Content-Type": "application/json; charset=utf-8"}
    if login:
        headers["Client-Login"] = login
    body = json.dumps({"method": "get", "params": params}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API + service, data=body, headers=headers)
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
    print(f"Client-Login в конфиге: {login}")

    import requests
    r = requests.get("https://login.yandex.ru/info", params={"format": "json"},
                     headers={"Authorization": "OAuth " + token}, timeout=30)
    print(f"\nвладелец токена: HTTP {r.status_code}")
    if r.status_code == 200:
        j = r.json()
        print(f"  логин {j.get('login')} | id {j.get('id')} | {j.get('default_email')}")

    res = call("clients", {"FieldNames": ["Login", "ClientId", "ClientInfo", "Type",
                                          "Representatives"]}, token, login)
    print("\nclients.get с Client-Login:")
    print(json.dumps(res, ensure_ascii=False, indent=2)[:1200])

    res2 = call("agencyclients", {"SelectionCriteria": {},
                                  "FieldNames": ["Login", "ClientId", "ClientInfo"],
                                  "Page": {"Limit": 50}}, token)
    print("\nagencyclients.get без Client-Login:")
    if res2.get("error"):
        print(f"  {res2['error'].get('error_string')} | {res2['error'].get('error_detail')}")
    else:
        for c in (res2.get("result") or {}).get("Clients", []):
            print(f"  {c.get('Login')} | {c.get('ClientInfo')}")


if __name__ == "__main__":
    main()
