# -*- coding: utf-8 -*-
"""Probe: «качественный бренд» для GSC = тотал − excludingRegex(бренд).
Неделя 33 (10–16.08). Ожидание: ae ≈ 20 807 показов / 1 925 кликов;
KZ ≈ 21 368 / 2 068. Read-only."""
import sys

sys.stdout.reconfigure(encoding="utf-8")

from sync.brand_terms import brand_regex
from sync.gsc import get_searchconsole_service

S, E = "2026-08-10", "2026-08-16"
service = get_searchconsole_service()


def q(site, filters=None):
    body = {"startDate": S, "endDate": E, "dimensions": ["date"],
            "rowLimit": 25000, "type": "web"}
    if filters:
        body["dimensionFilterGroups"] = [{"filters": filters}]
    rows = service.searchanalytics().query(siteUrl=site, body=body).execute().get("rows", [])
    return (sum(int(r["clicks"]) for r in rows), sum(int(r["impressions"]) for r in rows))

XF = lambda reg: {"dimension": "query", "operator": "excludingRegex", "expression": brand_regex(reg)}
CF = lambda c: {"dimension": "country", "operator": "equals", "expression": c}

for label, site, tot_f, nb_f in [
    ("ae (GCC)", "https://ae.limestore.com/", None, [XF("gcc")]),
    ("KZ", "https://limestore.com/", [CF("kaz")], [XF("kz"), CF("kaz")]),
]:
    tc, ti = q(site, tot_f)
    nc, ni = q(site, nb_f)
    print(f"{label}: тотал {tc} кл./{ti} пок.; видимый небренд {nc} кл./{ni} пок.")
    print(f"  → качественный бренд: {tc - nc} кл. / {ti - ni} пок.")
