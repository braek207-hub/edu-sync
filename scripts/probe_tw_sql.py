# -*- coding: utf-8 -*-
"""Зонд П2-e: SQL-эндпоинт Triple Whale — расход по странам и кампаниям.

⚠️ Первая версия этого зонда авторизовалась через `Authorization: Bearer` и получила
401 Invalid iss, из чего был сделан вывод «эндпоинт нам закрыт». Вывод неверный:
справочная страница (triplewhale.readme.io/reference/data-out-execute-custom-sql-query)
задаёт схему `{"type": "apiKey", "in": "header", "name": "x-api-key"}` — то есть
обычный ключ. Отличается и тело: shopId (не shopDomain), period.startDate/endDate,
а в самом SQL параметры @startDate / @endDate.

Только SELECT. Запуск: python -m scripts.probe_tw_sql
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

URL = "https://api.triplewhale.com/api/v2/orcabase/api/sql"
PERIOD = {"startDate": "2026-06-01", "endDate": "2026-06-30"}


def sql(query: str, label: str, show: int = 700):
    body = {
        "shopId": os.environ["GCC_TW_SHOP_DOMAIN"],
        "query": query,
        "period": PERIOD,
    }
    for attempt in range(1, 4):
        try:
            r = requests.post(
                URL,
                headers={"x-api-key": os.environ["GCC_TRIPLEWHALE_API_KEY"],
                         "Content-Type": "application/json"},
                json=body, timeout=180,
            )
        except Exception as exc:
            if attempt == 3:
                print(f"[{label}] сеть: {type(exc).__name__}")
                return None
            time.sleep(5 * attempt)
            continue
        break

    if r.status_code != 200:
        print(f"[{label}] HTTP {r.status_code}: {(r.text or '')[:280]}")
        return None
    try:
        data = r.json()
    except Exception:
        print(f"[{label}] 200, не JSON: {(r.text or '')[:200]}")
        return None
    print(f"[{label}] OK: {json.dumps(data, ensure_ascii=False)[:show]}")
    return data


def main():
    print("=== 1. Доступ (x-api-key, как в справочнике) ===")
    if sql("SELECT 1 AS ok", "смоук") is None:
        print("\nНе открылся и так — дальше по документации смотреть нечего.")
        return

    print("\n=== 2. Пример из документации: расход по каналам ===")
    sql("SELECT channel, SUM(spend) AS spend FROM ads_table "
        "WHERE event_date BETWEEN @startDate AND @endDate GROUP BY channel "
        "ORDER BY spend DESC", "ads_table по каналам")

    print("\n=== 3. Какие колонки есть у ads_table ===")
    sql("SELECT * FROM ads_table WHERE event_date BETWEEN @startDate AND @endDate LIMIT 1",
        "одна строка ads_table", show=1500)


if __name__ == "__main__":
    main()
