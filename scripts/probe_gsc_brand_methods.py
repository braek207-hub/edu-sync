# -*- coding: utf-8 -*-
"""Probe: из чего состоят ПОКАЗЫ ae.limestore.com (неделя 33) — бренд/небренд/анонимные,
топ небрендовых запросов по показам. Read-only."""
import sys

sys.stdout.reconfigure(encoding="utf-8")

from sync.brand_terms import is_brand_query
from sync.gsc import get_searchconsole_service

S, E = "2026-08-10", "2026-08-16"
SITE = "https://ae.limestore.com/"
service = get_searchconsole_service()


def q(dims, filters=None):
    body = {"startDate": S, "endDate": E, "dimensions": dims,
            "rowLimit": 25000, "type": "web"}
    if filters:
        body["dimensionFilterGroups"] = [{"filters": filters}]
    return service.searchanalytics().query(siteUrl=SITE, body=body).execute().get("rows", [])

rows = q(["query"])
tot = q(["date"])
tot_i = sum(int(r["impressions"]) for r in tot)
tot_c = sum(int(r["clicks"]) for r in tot)
vis_i = sum(int(r["impressions"]) for r in rows)
brand = [r for r in rows if is_brand_query(r["keys"][0], "gcc")]
nb = [r for r in rows if not is_brand_query(r["keys"][0], "gcc")]
brand_i = sum(int(r["impressions"]) for r in brand)
nb_i = sum(int(r["impressions"]) for r in nb)
nb_c = sum(int(r["clicks"]) for r in nb)
print(f"тотал: {tot_i} показов / {tot_c} кликов")
print(f"видимые запросы ({len(rows)} шт): показов {vis_i}")
print(f"  бренд: показов {brand_i}, небренд: показов {nb_i} / кликов {nb_c}")
print(f"анонимные (скрытые): показов {tot_i - vis_i}, кликов {tot_c - sum(int(r['clicks']) for r in rows)}")
print("\nтоп-25 НЕбрендовых по показам:")
for r in sorted(nb, key=lambda x: -int(x["impressions"]))[:25]:
    print(f"  {int(r['impressions']):>6} пок. {int(r['clicks']):>4} кл. поз.{float(r.get('position',0)):.0f}  {r['keys'][0]}")
