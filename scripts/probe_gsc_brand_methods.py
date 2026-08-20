# -*- coding: utf-8 -*-
"""Probe: что внутри «лишних» 373 кликов KZ (лист 2101 vs бренд 1728), неделя 33.

limestore.com, страна Казахстан: топ запросов, доля бренда среди видимых, доля анонимных.
Read-only. Запуск: workflow probe-gsc-methods.yml.
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

from sync.brand_terms import is_brand_query
from sync.gsc import get_searchconsole_service

S, E = "2026-08-10", "2026-08-16"
SITE = "https://limestore.com/"
CF = {"dimension": "country", "operator": "equals", "expression": "kaz"}
service = get_searchconsole_service()


def q(dims, filters):
    body = {"startDate": S, "endDate": E, "dimensions": dims,
            "rowLimit": 25000, "type": "web",
            "dimensionFilterGroups": [{"filters": filters}]}
    return service.searchanalytics().query(siteUrl=SITE, body=body).execute().get("rows", [])

print(f"limestore.com, страна Казахстан, {S}..{E}\n")

rows = q(["query"], [CF])
vis_c = sum(int(r["clicks"]) for r in rows)
vis_i = sum(int(r["impressions"]) for r in rows)
tot = q(["date"], [CF])
tot_c = sum(int(r["clicks"]) for r in tot)
tot_i = sum(int(r["impressions"]) for r in tot)
brand_c = sum(int(r["clicks"]) for r in rows if is_brand_query(r["keys"][0], "kz"))
print(f"тотал (метод листа): clicks={tot_c}, imps={tot_i}")
print(f"видимые запросы: clicks={vis_c} (бренд {brand_c}, небренд {vis_c - brand_c}), imps={vis_i}")
print(f"анонимные (скрытые GSC): clicks={tot_c - vis_c}, imps={tot_i - vis_i}\n")

print("== топ-25 запросов по кликам ==")
for r in sorted(rows, key=lambda x: -int(x["clicks"]))[:25]:
    query = r["keys"][0]
    mark = "БРЕНД" if is_brand_query(query, "kz") else "  —  "
    print(f"  [{mark}] {int(r['clicks']):>5} кл. {int(r['impressions']):>7} пок.  {query}")

print("\n== топ-15 НЕбрендовых запросов ==")
nb = [r for r in rows if not is_brand_query(r["keys"][0], "kz")]
for r in sorted(nb, key=lambda x: -int(x["clicks"]))[:15]:
    print(f"  {int(r['clicks']):>5} кл. {int(r['impressions']):>7} пок.  {r['keys'][0]}")
