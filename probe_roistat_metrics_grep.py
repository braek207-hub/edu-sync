# -*- coding: utf-8 -*-
"""Из официального списка метрик Роистата выбрать всё про новизну клиента и деньги.

Отвечает на вопрос: можно ли получить выручку/средний чек в разрезе «новый / повторный»,
или API отдаёт только количества продаж (new_sales / repeatedSales).
"""
import io
import json
import os
import sys
import urllib.parse
import urllib.request

from dotenv import load_dotenv

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
load_dotenv()

qs = urllib.parse.urlencode({
    "key": os.environ["ROISTAT_API_KEY"],
    "project": os.environ.get("ROISTAT_PROJECT_ID") or "235593",
})
req = urllib.request.Request(
    f"https://cloud.roistat.com/api/v1/project/analytics/metrics?{qs}", method="GET"
)
with urllib.request.urlopen(req, timeout=60) as r:
    metrics = json.loads(r.read().decode("utf-8"))["metrics"]

print(f"всего метрик: {len(metrics)}\n")

NEWNESS = ("new", "repeat", "перв", "повтор", "нов")
MONEY = ("price", "revenue", "sum", "выручк", "доход", "чек", "сумм")

print("── новизна клиента ──")
for m in metrics:
    hay = f"{m['name']} {m.get('title') or ''} {m.get('info') or ''}".lower()
    if any(k in hay for k in NEWNESS):
        avail = "" if m.get("is_available") else "  (недоступна)"
        print(f"  {m['name']:26} {m.get('type'):8} {m.get('title')}{avail}")

print("\n── деньги ──")
for m in metrics:
    hay = f"{m['name']} {m.get('title') or ''}".lower()
    if any(k in hay for k in MONEY):
        avail = "" if m.get("is_available") else "  (недоступна)"
        print(f"  {m['name']:26} {m.get('type'):8} {m.get('title')}{avail}")
