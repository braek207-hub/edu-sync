# -*- coding: utf-8 -*-
"""Разовый зонд: отдаёт ли счётчик Метрики GCC (98232701) ecommerce-заказы/выручку.

Нужен для решения «Метрика GCC вторым источником заказов в дашборде» (Павел, 2026-08-20):
если ecommerce на счётчике не включён — колонки заказов/выручки Метрики на GCC будут пустыми,
и тянуть стоит только визиты/пользователей. Печатает ТОЛЬКО агрегаты, без секретов.

Запуск: python scripts/probe_gcc_metrika_ecommerce.py (локально нужен GCC_METRICA_TOKEN в .env)
"""
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ.get("GCC_METRICA_TOKEN")
COUNTER = os.environ.get("GCC_METRICA_COUNTER_ID") or "98232701"

if not TOKEN:
    print("GCC_METRICA_TOKEN не задан — зонд не запущен")
    sys.exit(1)

resp = requests.get(
    "https://api-metrika.yandex.net/stat/v1/data",
    params={
        "ids": COUNTER,
        "date1": "2026-08-04",
        "date2": "2026-08-10",
        "metrics": "ym:s:visits,ym:s:ecommercePurchases,ym:s:ecommerceRevenue",
        "dimensions": "ym:s:date",
        "limit": "100",
        "accuracy": "full",
    },
    headers={"Authorization": f"OAuth {TOKEN}"},
    timeout=60,
)
resp.raise_for_status()
data = resp.json()
totals = data.get("totals") or [0, 0, 0]
if totals and isinstance(totals[0], list):
    totals = totals[0]
print(f"counter={COUNTER} 2026-08-04..10: visits={totals[0]:.0f} "
      f"purchases={totals[1]:.0f} revenue={totals[2]:.0f}")
for row in data.get("data", [])[:3]:
    d = row["dimensions"][0]["name"]
    m = row["metrics"]
    print(f"  {d}: visits={m[0]:.0f} purchases={m[1]:.0f} revenue={m[2]:.0f}")
