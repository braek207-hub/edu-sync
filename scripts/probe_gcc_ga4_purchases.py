# -*- coding: utf-8 -*-
"""Разовый зонд: отдаёт ли GA4 GCC (property 417919368) ecommerce-заказы/выручку.

Для «GA4-заказы вторым источником в дашборде» (Павел, 2026-08-20). Печатает только агрегаты.
Запуск: python scripts/probe_gcc_ga4_purchases.py (нужен SA-ключ lime-reports в .env)
"""
import sys

from dotenv import load_dotenv

load_dotenv()

from sync.gcc_ga4 import GA4_PROPERTY, run_report  # noqa: E402

try:
    rows = run_report(
        GA4_PROPERTY, "2026-08-04", "2026-08-10",
        dimensions=["date"],
        metrics=("ecommercePurchases", "purchaseRevenue", "transactions"),
    )
except Exception as e:  # noqa: BLE001
    print(f"GA4 запрос упал: {type(e).__name__}: {e}")
    sys.exit(1)

tot_p = tot_r = tot_t = 0.0
for r in sorted(rows, key=lambda x: x["dims"][0])[:10]:
    p, rev, t = (float(v) for v in r["metrics"])
    tot_p += p; tot_r += rev; tot_t += t
    print(f"  {r['dims'][0]}: purchases={p:.0f} revenue={rev:.0f} transactions={t:.0f}")
print(f"TOTAL 04-10.08: purchases={tot_p:.0f} revenue={tot_r:.0f} transactions={tot_t:.0f}")
