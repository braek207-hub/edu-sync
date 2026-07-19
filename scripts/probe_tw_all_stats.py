# -*- coding: utf-8 -*-
"""Зонд П2-d: attribution/get-all-stats — эндпоинт за экраном Attribution.

Путь дал Павел из вкладки Network: POST https://app.triplewhale.com/api/v2/attribution/
get-all-stats. Хост app.*, а не api.* — сам бы не угадала.

Задача: (1) пускает ли он наш x-api-key, (2) каким полем принимает фильтр по стране.
Оракул — интерфейс за Jan 1 – Jul 12 2026 по стране QA: Google 1 982,48 AED,
Meta 10 543,76 AED, 216 покупок. Кандидат засчитывается только при совпадении с ним.

Только чтение. Запуск: python -m scripts.probe_tw_all_stats
"""
import io
import json
import os
import sys
import time

import requests
from dotenv import load_dotenv

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
load_dotenv()

PATH = "/api/v2/attribution/get-all-stats"
START, END = "2026-01-01", "2026-07-12"
EXPECT = {"google": 1982.48, "meta": 10543.76, "purchases": 216}


def call(host: str, body: dict, auth: str = "x-api-key"):
    key = os.environ["GCC_TRIPLEWHALE_API_KEY"]
    headers = {"content-type": "application/json"}
    if auth == "x-api-key":
        headers["x-api-key"] = key
    else:
        headers["Authorization"] = f"Bearer {key}"
    for attempt in range(1, 4):
        try:
            r = requests.post(f"https://{host}{PATH}", headers=headers, json=body, timeout=180)
        except Exception as exc:
            if attempt == 3:
                return None, f"сеть: {type(exc).__name__}"
            time.sleep(5 * attempt)
            continue
        if r.status_code != 200:
            return None, f"HTTP {r.status_code} {(r.text or '')[:140]}"
        try:
            return r.json(), None
        except Exception:
            return None, "200, но не JSON"
    return None, "не достучались"


def main():
    shop = os.environ["GCC_TW_SHOP_DOMAIN"]
    base = {"shopDomain": shop, "shop-id": shop, "shopId": shop,
            "startDate": START, "endDate": END,
            "start": START, "end": END,
            "model": "lastPlatformClick"}

    print("=== 1. Доступ и авторизация ===")
    ok_host = ok_auth = None
    for host in ("app.triplewhale.com", "api.triplewhale.com"):
        for auth in ("x-api-key", "bearer"):
            data, err = call(host, base, auth)
            status = "OK" if data is not None else err
            print(f"  {host:<24} {auth:<10} {status}")
            if data is not None and ok_host is None:
                ok_host, ok_auth = host, auth
                print(f"      форма ответа: {json.dumps(data, ensure_ascii=False)[:400]}")
            time.sleep(2)

    if not ok_host:
        print("\nЭндпоинт нашим ключом не открывается — нужен токен сессии из браузера.")
        return

    print(f"\n=== 2. Фильтр по стране (host={ok_host}, auth={ok_auth}) ===")
    candidates = {
        "countries": {"countries": ["QA"]},
        "country": {"country": "QA"},
        "countryCode": {"countryCode": "QA"},
        "countryCodes": {"countryCodes": ["QA"]},
        "filters.country": {"filters": {"country": ["QA"]}},
        "filters.list": {"filters": [{"key": "country", "value": ["QA"]}]},
        "geo": {"geo": ["QA"]},
        "selectedCountries": {"selectedCountries": ["QA"]},
    }
    for name, extra in candidates.items():
        time.sleep(3)
        data, err = call(ok_host, {**base, **extra}, ok_auth)
        if err:
            print(f"  {name:<20} {err}")
            continue
        blob = json.dumps(data, ensure_ascii=False)
        hit = all(str(int(v)) in blob or f"{v:.2f}" in blob for v in
                  (EXPECT["google"], EXPECT["meta"]))
        print(f"  {name:<20} ответ {len(blob)} симв.  {'✓ ЦИФРЫ СОШЛИСЬ' if hit else ''}")
        if hit:
            print(f"      {blob[:600]}")


if __name__ == "__main__":
    main()
