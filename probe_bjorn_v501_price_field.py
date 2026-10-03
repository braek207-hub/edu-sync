"""Есть ли у RESPONSIVE_AD в v501 поле цены (старая/новая).

В интерфейсе у РСЯ-объявления блок «Цена» есть, в v5 поля нет. Спрашиваем сам Директ:
посылаем заведомо неверное значение в ResponsiveAdFieldNames — ошибка перечислит
все допустимые поля. Только чтение.
"""

from __future__ import annotations

import json
import os
import sys

import requests

EXPECTED_LOGIN_PART = "bjorn"
CAMPAIGN = 715011487


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

    for version in ("v501", "v5"):
        res = call(version, "ads", {
            "SelectionCriteria": {"CampaignIds": [CAMPAIGN]},
            "FieldNames": ["Id"],
            "ResponsiveAdFieldNames": ["ЗАВЕДОМО_НЕВЕРНОЕ"],
            "Page": {"Limit": 1}}, login, token)
        err = res.get("error") or {}
        print(f"\n--- {version} ResponsiveAdFieldNames: {err.get('error_string')}")
        print(f"{err.get('error_detail')}")

    # и сам список полей объявления на запись: тоже спрашиваем ошибкой
    res = call("v501", "ads", {"Ads": [{"Id": 1, "ResponsiveAd": {
        "ЗАВЕДОМО_НЕВЕРНОЕ": 1}}]}, login, token, "update")
    err = res.get("error") or {}
    print(f"\n--- v501 ResponsiveAd на запись: {err.get('error_string')}")
    print(f"{err.get('error_detail')}")
    if not err:
        print(json.dumps(res, ensure_ascii=False)[:500])
    sys.stdout.flush()


if __name__ == "__main__":
    main()
