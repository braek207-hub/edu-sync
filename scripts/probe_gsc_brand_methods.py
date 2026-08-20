# -*- coding: utf-8 -*-
"""Probe: что внутри «небрендового» трафика ae.limestore.com (неделя 33, 10–16.08.2026).

1) Топ-30 запросов витрины без фильтров (клики/показы, пометка бренд/нет).
2) Брендовые клики по странам пользователя.
3) Сумма кликов по запросам vs тотал без группировки (доля анонимных).
Read-only. Запуск: workflow probe-gsc-methods.yml (переиспользуем шаг).
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

from sync.brand_terms import brand_regex, is_brand_query
from sync.gsc import get_searchconsole_service

S, E = "2026-08-10", "2026-08-16"
SITE = "https://ae.limestore.com/"
service = get_searchconsole_service()


def q(dims, filters=None, limit=25000):
    body = {"startDate": S, "endDate": E, "dimensions": dims,
            "rowLimit": limit, "type": "web"}
    if filters:
        body["dimensionFilterGroups"] = [{"filters": filters}]
    return service.searchanalytics().query(siteUrl=SITE, body=body).execute().get("rows", [])

print(f"ae.limestore.com, {S}..{E}\n")

rows = q(["query"])
total_grouped_clicks = sum(int(r["clicks"]) for r in rows)
total_grouped_imps = sum(int(r["impressions"]) for r in rows)
tot = q(["date"])
total_clicks = sum(int(r["clicks"]) for r in tot)
total_imps = sum(int(r["impressions"]) for r in tot)
print(f"тотал без группировки: clicks={total_clicks}, imps={total_imps}")
print(f"сумма по видимым запросам: clicks={total_grouped_clicks}, imps={total_grouped_imps}")
print(f"анонимные (скрытые GSC): clicks={total_clicks - total_grouped_clicks}, "
      f"imps={total_imps - total_grouped_imps}\n")

brand_c = sum(int(r["clicks"]) for r in rows if is_brand_query(r["keys"][0], "gcc"))
print(f"из видимых запросов брендовых кликов: {brand_c}, небрендовых: {total_grouped_clicks - brand_c}\n")

print("== топ-30 запросов по кликам (все страны) ==")
for r in sorted(rows, key=lambda x: -int(x["clicks"]))[:30]:
    query = r["keys"][0]
    mark = "БРЕНД" if is_brand_query(query, "gcc") else "  —  "
    print(f"  [{mark}] {int(r['clicks']):>5} кл. {int(r['impressions']):>7} пок.  {query}")

print("\n== брендовые клики по странам пользователя ==")
crows = q(["country"], [{"dimension": "query", "operator": "includingRegex",
                         "expression": brand_regex("gcc")}])
for r in sorted(crows, key=lambda x: -int(x["clicks"]))[:15]:
    print(f"  {r['keys'][0]}: clicks={int(r['clicks'])}, imps={int(r['impressions'])}")
