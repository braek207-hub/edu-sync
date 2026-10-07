# -*- coding: utf-8 -*-
"""Probe: что маска бренда (brand_regex — подстроки «lim», «ليم») засчитывает в бренд лишнего.

По каждой паре ресурс×страна: запросы, попавшие под маску, делятся на
- строгий бренд (написание отдельным словом),
- чужие бренды с тем же словом (limelight, limeroad, lime gardens…),
- только подстрока (slim, limit, sublime, تعليم…) — ложные срабатывания.
Период 07.09–04.10.2026. Read-only."""
import re
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8")

from sync.brand_terms import brand_regex
from sync.gsc import get_searchconsole_service

S, E = "2026-09-07", "2026-10-04"
service = get_searchconsole_service()
ROOT = "https://limestore.com/"
PAIRS = [
    (ROOT, "kaz", "kz"), ("https://ae.limestore.com/", "are", "gcc"),
    ("https://sa.limestore.com/", "sau", "gcc"), ("https://kw.limestore.com/", "kwt", "gcc"),
    ("https://qa.limestore.com/", "qat", "gcc"), ("https://om.limestore.com/", "omn", "gcc"),
    ("https://bh.limestore.com/", "bhr", "gcc"), (ROOT, "are", "gcc"), (ROOT, "sau", "gcc"),
]
L = r"a-zа-яё"
STRICT = re.compile(
    rf"(?i)(?<![{L}])(lime|limé|limestore|lim|laim|leem|лайм|лаим|лиме|дшьу|kfqv)(?![{L}])"
    r"|لايم|(?<![؀-ۿ])ليم(?![؀-ۿ])")
FOREIGN = re.compile(r"(?i)limelight|limeroad|lime ?garden|lime ?scooter|lime ?bike|key lime|"
                     r"lime ?juice|lime ?tree|lime ?green|lime ?wash|lime ?stone|limestone")


def rows(site, country, region):
    out, start = [], 0
    while True:
        body = {"startDate": S, "endDate": E, "dimensions": ["query"], "rowLimit": 25000,
                "startRow": start, "type": "web", "dimensionFilterGroups": [{"filters": [
                    {"dimension": "country", "operator": "equals", "expression": country},
                    {"dimension": "query", "operator": "includingRegex",
                     "expression": brand_regex(region)}]}]}
        page = service.searchanalytics().query(siteUrl=site, body=body).execute().get("rows", [])
        out += page
        if len(page) < 25000:
            return out
        start += 25000


for site, country, region in PAIRS:
    groups = defaultdict(lambda: [0, 0, []])
    for r in rows(site, country, region):
        q = r["keys"][0]
        g = "foreign" if FOREIGN.search(q) else ("strict" if STRICT.search(q) else "substring")
        groups[g][0] += int(r["clicks"])
        groups[g][1] += int(r["impressions"])
        groups[g][2].append((int(r["impressions"]), int(r["clicks"]), q))
    total = sum(v[1] for v in groups.values()) or 1
    print(f"\n=== {site.split('//')[1]} × {country}: всего под маской {total} показов")
    for g in ("strict", "foreign", "substring"):
        c, i, _ = groups[g]
        print(f"  {g:9}: {c} кл / {i} пок ({100 * i / total:.1f}% показов)")
    for g in ("foreign", "substring"):
        top = sorted(groups[g][2], reverse=True)[:25]
        if top:
            print(f"  топ {g}: " + "; ".join(f"{q} {i}/{c}" for i, c, q in top))
