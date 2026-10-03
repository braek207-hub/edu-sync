"""Сверка состава ТГО 715011487 с образцом 714000003 — что потерял update через v5.

Заголовки уже восстановлены, тексты целы. Здесь проверяем остальное: картинки, видео,
уточнения, кнопку, карусель, быстрые ссылки, организацию. Сопоставление по Href.
Только чтение.
"""

from __future__ import annotations

import json
import os
import sys

import requests

DUP = 715011487
SOURCE = 714000003
EXPECTED_LOGIN_PART = "bjorn"

FIELDS = ["Titles", "Texts", "Href", "AdImages", "VideoExtensions", "AdExtensions",
          "ButtonExtension", "Carousel", "SitelinkSetId", "BusinessId",
          "DisplayUrlPath", "PriceExtension", "TrackingParams"]


def call(service: str, params: dict, login: str, token: str, method: str = "get") -> dict:
    url = f"https://api.direct.yandex.com/json/v501/{service}"
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


def need(body: dict, what: str) -> dict:
    e = body.get("error")
    if e:
        sys.exit(f"СТОП {what}: {e.get('error_string')} | {e.get('error_detail')}")
    return body.get("result", {})


def size(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, list):
        return str(len(value))
    if isinstance(value, dict):
        return "есть"
    return "есть"


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

    def read(camp: int) -> list[dict]:
        res = need(call("ads", {
            "SelectionCriteria": {"CampaignIds": [camp]},
            "FieldNames": ["Id", "AdGroupId", "Type"],
            "ResponsiveAdFieldNames": FIELDS,
            "Page": {"Limit": 1000}}, login, token), f"объявления {camp}")
        return [a for a in res.get("Ads", []) if a.get("Type") == "RESPONSIVE_AD"]

    dup, src = read(DUP), read(SOURCE)
    by_href = {}
    for a in src:
        href = (a.get("ResponsiveAd") or {}).get("Href") or ""
        by_href.setdefault(href, a)
    print(f"дубль {len(dup)} | образец {len(src)}\n")

    diffs: dict = {}
    for a in sorted(dup, key=lambda x: x["Id"]):
        ra = a.get("ResponsiveAd") or {}
        origin = (by_href.get(ra.get("Href") or "") or {}).get("ResponsiveAd") or {}
        if not origin:
            print(f"  ! {a['Id']} нет пары в образце")
            continue
        for f in FIELDS:
            if f in ("Titles", "PriceExtension"):      # мы их намеренно заменили
                continue
            mine, theirs = size(ra.get(f)), size(origin.get(f))
            if mine != theirs:
                diffs.setdefault(f, []).append((a["Id"], theirs, mine))

    if not diffs:
        print("состав объявлений совпадает с образцом по всем полям, кроме заменённых "
              "намеренно (заголовки и цена)")
    else:
        print("расхождения с образцом:")
        for f, rows in diffs.items():
            print(f"  {f}: {len(rows)} объявлений (образец -> дубль)")
            for aid, theirs, mine in rows[:4]:
                print(f"    {aid}: {theirs} -> {mine}")

    print("\nсводка по дублю:")
    for f in FIELDS:
        acc: dict = {}
        for a in dup:
            acc[size((a.get("ResponsiveAd") or {}).get(f))] = \
                acc.get(size((a.get("ResponsiveAd") or {}).get(f)), 0) + 1
        print(f"  {f}: {acc}")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
