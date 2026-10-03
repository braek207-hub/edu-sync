"""Цена Бомбера Котлас в 715011487: карточка отдаёт 404, берём унаследованную.

Карточка https://bjornlarsen.ru/product/muzhskaya-kurtka-bomber-iz-naturalnoy-kozhi-
kotlas-chyernaya/ отвечает 404 — объявление ведёт в пустоту (та же ссылка стоит и в
работающей кампании-образце 714000003). Цену с сайта взять нельзя, поэтому «старой»
считаем ту, что досталась объявлению при клонировании, и от неё считаем −15%.

APPLY=1 — писать.
"""

from __future__ import annotations

import json
import os
import sys

import requests

CAMPAIGN = 715011487
SLUG = "kotlas"
DISCOUNT = 0.15
EXPECTED_LOGIN_PART = "bjorn"


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


def main() -> None:
    apply = os.environ.get("APPLY", "").strip() == "1"
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
    print(f"кабинет {login} | режим: {'ЗАПИСЬ' if apply else 'план'}")

    res = need(call("ads", {
        "SelectionCriteria": {"CampaignIds": [CAMPAIGN]},
        "FieldNames": ["Id", "Type"],
        "ResponsiveAdFieldNames": ["Titles", "Href", "PriceExtension"],
        "Page": {"Limit": 1000}}, login, token), "объявления")
    target = None
    for a in res.get("Ads", []):
        ra = a.get("ResponsiveAd") or {}
        if a.get("Type") == "RESPONSIVE_AD" and SLUG in (ra.get("Href") or ""):
            target = a
            break
    if not target:
        sys.exit(f"объявление со slug «{SLUG}» не найдено")

    ra = target["ResponsiveAd"]
    pe = ra.get("PriceExtension") or {}
    print(f"  {target['Id']} «{(ra.get('Titles') or [{}])[0].get('Title')}»")
    print(f"  текущий блок цены: {json.dumps(pe, ensure_ascii=False)}")

    micro = max([v for v in (pe.get("Price"), pe.get("OldPrice")) if v] or [0])
    if not micro:
        sys.exit("унаследованной цены нет — ставить не от чего")
    old = int(micro // 1_000_000)
    new = int(old * (1 - DISCOUNT) // 10 * 10)
    print(f"  старая {old} -> новая {new}")

    if not apply:
        print("без APPLY=1 ничего не меняю")
        return

    out = need(call("ads", {"Ads": [{"Id": target["Id"], "ResponsiveAd": {"PriceExtension": {
        "Price": new * 1_000_000, "OldPrice": old * 1_000_000,
        "PriceCurrency": "RUB", "PriceQualifier": "NONE"}}}]},
        login, token, "update"), "запись цены")
    r0 = (out.get("UpdateResults") or [{}])[0]
    print(f"  ответ: {json.dumps(r0, ensure_ascii=False)}")

    chk = need(call("ads", {
        "SelectionCriteria": {"Ids": [target["Id"]]},
        "FieldNames": ["Id"], "ResponsiveAdFieldNames": ["PriceExtension"]},
        login, token), "сверка")
    for a in chk.get("Ads", []):
        print(f"  прочитано обратно: "
              f"{json.dumps((a.get('ResponsiveAd') or {}).get('PriceExtension'), ensure_ascii=False)}")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
