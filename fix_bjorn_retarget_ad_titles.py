"""Перезаписывает заголовки объявления ретаргет-кампании BJORN 714884936.

Причина: Директ теперь создаёт текстовые баннеры как комбинаторные, а у них Title и Title2
делят общий лимит 56 символов. Первая версия (43 + 25) в него не влезла — Title2 молча
не применился (предупреждение 10254). Делим 26 + 22.

После правки объявление уходит на модерацию заново и результат проверяется чтением.
"""

from __future__ import annotations

import json
import os
import sys

import requests

API = "https://api.direct.yandex.com/json/v5/"
AD_ID = 1922707353828967818
NEW_TITLE = "Зимние куртки BJORN LARSEN"
NEW_TITLE2 = "Скидка до -20% на зиму"


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


def main() -> None:
    assert len(NEW_TITLE) + len(NEW_TITLE2) <= 56, "не влезет в общий лимит комбинаторного"
    for login, token in clients():
        if not token:
            continue
        if "bjorn" not in login.lower():
            print(f"логин «{login}» не похож на BJORN — стоп")
            sys.exit(1)
        print(f"аккаунт {login}, объявление {AD_ID}")

        upd = call("ads", {"Ads": [{
            "Id": AD_ID,
            "TextAd": {"Title": NEW_TITLE, "Title2": NEW_TITLE2},
        }]}, login, token, "update")
        print("update: " + json.dumps(upd.get("result") or upd, ensure_ascii=False)[:600])
        if upd.get("error"):
            sys.exit(1)

        mod = call("ads", {"SelectionCriteria": {"Ids": [AD_ID]}}, login, token, "moderate")
        print("moderate: " + json.dumps(mod.get("result") or mod, ensure_ascii=False)[:400])

        chk = call("ads", {
            "SelectionCriteria": {"Ids": [AD_ID]},
            "FieldNames": ["Id", "State", "Status", "StatusClarification"],
            "TextAdFieldNames": ["Title", "Title2", "Text", "SitelinkSetId", "AdImageHash",
                                 "VideoExtension", "AdExtensions"],
            "Page": {"Limit": 1},
        }, login, token)
        ads = chk.get("result", {}).get("Ads", [])
        if ads:
            t = ads[0].get("TextAd", {})
            print(f"\nпроверка: Title «{t.get('Title')}» ({len(t.get('Title') or '')}) | "
                  f"Title2 «{t.get('Title2')}» ({len(t.get('Title2') or '')}) | "
                  f"{ads[0].get('Status')} / {ads[0].get('StatusClarification')}")
            print(f"расширения: ссылки={t.get('SitelinkSetId')} картинка={t.get('AdImageHash')} "
                  f"уточнений={len(t.get('AdExtensions') or [])} "
                  f"видео={(t.get('VideoExtension') or {}).get('CreativeId')}")
        else:
            print(json.dumps(chk, ensure_ascii=False)[:600])
        return
    print("нет доступа")
    sys.exit(1)


if __name__ == "__main__":
    main()
