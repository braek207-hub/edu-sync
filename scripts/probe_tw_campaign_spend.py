# -*- coding: utf-8 -*-
"""Зонд П2: есть ли у Triple Whale расход Meta по кампаниям/странам.

Мотив: `summary-page` отдаёт fb_ads_spend одной цифрой на весь магазин, поэтому 61%
расхода GCC идёт строкой без страны и не мержится с трафиком. Прежде чем строить
интеграцию с Meta Marketing API (новый токен + приложение), проверяем, не отдаёт ли
эти данные сам TW — на его экране Attribution расход по кампаниям виден, значит
эндпоинт существует.

Только чтение. Печатает коды ответов и ФОРМУ данных, не содержимое ключа.
Запуск: python -m scripts.probe_tw_campaign_spend
"""
import io
import json
import os
import sys
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
load_dotenv()

BASE = "https://api.triplewhale.com/api/v2"
to = date.today() - timedelta(days=1)
frm = to - timedelta(days=6)
D1, D2 = frm.isoformat(), to.isoformat()


def probe(name: str, path: str, body: dict, method: str = "POST"):
    key = os.environ["GCC_TRIPLEWHALE_API_KEY"]
    url = f"{BASE}{path}"
    headers = {"x-api-key": key, "content-type": "application/json"}
    try:
        if method == "GET":
            r = requests.get(url, headers=headers, params=body, timeout=60)
        else:
            r = requests.post(url, headers=headers, json=body, timeout=60)
    except Exception as exc:
        print(f"  {name:<38} ИСКЛЮЧЕНИЕ {type(exc).__name__}")
        return None

    status = r.status_code
    if status != 200:
        snippet = (r.text or "")[:120].replace("\n", " ")
        print(f"  {name:<38} HTTP {status}  {snippet}")
        return None

    try:
        data = r.json()
    except Exception:
        print(f"  {name:<38} HTTP 200, но не JSON")
        return None

    if isinstance(data, dict):
        keys = list(data.keys())[:8]
        print(f"  {name:<38} HTTP 200  ключи: {keys}")
    elif isinstance(data, list):
        print(f"  {name:<38} HTTP 200  список, {len(data)} элементов")
        if data:
            print(f"      элемент: {json.dumps(data[0], ensure_ascii=False)[:260]}")
    return data


def main():
    shop = os.environ["GCC_TW_SHOP_DOMAIN"]
    print(f"[зонд П2] период {D1}…{D2}\n")

    print("Кандидаты на расход по кампаниям:")
    candidates = [
        ("attribution/get-ads-data", {"shop": shop, "startDate": D1, "endDate": D2}),
        ("attribution/get-attribution", {"shop": shop, "startDate": D1, "endDate": D2,
                                         "model": "lastPlatformClick"}),
        ("attribution/get-campaigns", {"shop": shop, "startDate": D1, "endDate": D2}),
        ("metrics/get-data", {"shopDomain": shop, "period": {"start": D1, "end": D2}}),
        ("ads/get-campaigns", {"shop": shop, "startDate": D1, "endDate": D2}),
    ]
    found = {}
    for path, body in candidates:
        result = probe(path, f"/{path}", body)
        if result:
            found[path] = result

    # summary-page: вдруг принимает разбивку, о которой мы не знали
    print("\nsummary-page с попыткой разбивки:")
    for extra in ({"breakdown": "country"}, {"groupBy": "campaign"},
                  {"breakdowns": ["country"]}):
        body = {"shopDomain": shop, "period": {"start": D1, "end": D2},
                "todayHour": 25, **extra}
        probe(f"summary-page {list(extra)[0]}", "/summary-page/get-data", body)

    # Есть ли вообще у ключа доступ к чему-то ещё
    print("\nСлужебные:")
    probe("users/api-keys/me", "/users/api-keys/me", {}, method="GET")

    if found:
        print("\n=== Разбор найденного ===")
        for path, data in found.items():
            print(f"\n{path}:")
            print(json.dumps(data, ensure_ascii=False)[:900])


if __name__ == "__main__":
    main()
