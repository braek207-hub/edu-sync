# -*- coding: utf-8 -*-
"""Probe: почему CTR «качественного бренда» GSC ~10% (ae, KZ) и 2–4% (остальной GCC).

Разбивка показов ряда «тотал − видимый небренд» на: видимый бренд по классам запросов
(чистый бренд / «lim» внутри чужого слова / lime-цвет-фрукт / lime + товар), анонимные,
страны пользователей, устройства, позиции. 4 полные недели 07.09–04.10.2026. Read-only."""
import re
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8")

from sync.brand_terms import brand_regex
from sync.gsc import REGIONS, get_searchconsole_service

S, E = "2026-09-07", "2026-10-04"
service = get_searchconsole_service()

INC = lambda reg: {"dimension": "query", "operator": "includingRegex", "expression": brand_regex(reg)}
EXC = lambda reg: {"dimension": "query", "operator": "excludingRegex", "expression": brand_regex(reg)}
CF = lambda c: {"dimension": "country", "operator": "equals", "expression": c}


def q(site, dims, filters):
    body = {"startDate": S, "endDate": E, "dimensions": dims, "rowLimit": 25000, "type": "web"}
    if filters:
        body["dimensionFilterGroups"] = [{"filters": filters}]
    return service.searchanalytics().query(siteUrl=site, body=body).execute().get("rows", [])


def tot(rows):
    return sum(int(r["clicks"]) for r in rows), sum(int(r["impressions"]) for r in rows)


def ctr(c, i):
    return f"{100 * c / i:.1f}%" if i else "—"


# Слова, где «lim» — часть чужого слова (фильтр бренда ловит подстроку «lim»).
REAL_BRAND = re.compile(r"(?i)(lime|limé|laim|лайм|лаим|лиме|дшьу|kfqv|leem|لايم|ليم|\blim\b)")
GENERIC = re.compile(r"(?i)\b(green|colou?r|juice|fruit|tree|lemon|neon|yellow|cordial|pickle|soda|"
                     r"mint|leaf|zest|key lime|kaffir|pie|light|lime ?stone|squeeze|water|drink|"
                     r"اخضر|أخضر|ليمون|زيتي|فسفوري)\b")
PURE = re.compile(r"(?i)^\s*(lime|limé|лайм|лаим|لايم|ليم|laim|leem)\s*"
                  r"(store|shop|official|online|website|site|com|\.com|uae|dubai|abu dhabi|ksa|saudi|"
                  r"kuwait|qatar|doha|oman|muscat|bahrain|riyadh|jeddah|kz|kazakhstan|almaty|astana|"
                  r"казахстан|алматы|астана|кз|магазин|официальный|сайт|اونلاين|متجر|الامارات|دبي)?\s*$"
                  r"|limestore|lime-shop|lime shop|lime store")


def klass(query):
    if not REAL_BRAND.search(query):
        return "lim внутри чужого слова"
    if PURE.search(query):
        return "чистый бренд"
    if GENERIC.search(query):
        return "lime = цвет/фрукт"
    return "lime + товар/прочее"


def probe(label, site, region, country):
    cf = [CF(country)] if country else []
    tc, ti = tot(q(site, ["date"], cf))
    nc, ni = tot(q(site, ["date"], cf + [EXC(region)]))
    bc, bi = tot(q(site, ["date"], cf + [INC(region)]))
    ac, ai = tc - nc - bc, ti - ni - bi
    qc, qi = tc - nc, ti - ni
    print(f"\n=== {label}  ({site}{' country=' + country if country else ''})")
    print(f"тотал {tc}/{ti} {ctr(tc, ti)} | видимый небренд {nc}/{ni} {ctr(nc, ni)}")
    print(f"РЯД (бренд+аноним) {qc}/{qi} {ctr(qc, qi)} = видимый бренд {bc}/{bi} {ctr(bc, bi)}"
          f" + анонимные {ac}/{ai} {ctr(ac, ai)}  (аноним = {100 * ai / qi if qi else 0:.0f}% показов ряда)")

    rows = q(site, ["query"], cf + [INC(region)])
    agg = defaultdict(lambda: [0, 0, 0.0, 0])
    for r in rows:
        k = klass(r["keys"][0])
        a = agg[k]
        a[0] += int(r["clicks"]); a[1] += int(r["impressions"])
        a[2] += r["position"] * r["impressions"]; a[3] += 1
    print("видимый бренд по классам (клики/показы, CTR, ср.позиция, запросов):")
    for k, (c, i, p, n) in sorted(agg.items(), key=lambda x: -x[1][1]):
        print(f"  {k:26s} {c:6d}/{i:7d} {ctr(c, i):>6s} поз.{p / i if i else 0:5.1f}  n={n}")
    print("топ-25 брендовых запросов по показам:")
    for r in sorted(rows, key=lambda r: -r["impressions"])[:25]:
        print(f"  {r['keys'][0][:45]:45s} {int(r['clicks']):5d}/{int(r['impressions']):6d} "
              f"{ctr(r['clicks'], r['impressions']):>6s} поз.{r['position']:5.1f}  [{klass(r['keys'][0])}]")
    print("топ-10 запросов с CTR<2% и показами>=200:")
    low = [r for r in rows if r["impressions"] >= 200 and r["clicks"] / r["impressions"] < 0.02]
    for r in sorted(low, key=lambda r: -r["impressions"])[:10]:
        print(f"  {r['keys'][0][:45]:45s} {int(r['clicks']):5d}/{int(r['impressions']):6d} поз.{r['position']:5.1f}")

    if not country:
        t = {r["keys"][0]: r for r in q(site, ["country"], [])}
        n = {r["keys"][0]: r for r in q(site, ["country"], [EXC(region)])}
        print("ряд по странам пользователя (топ-8 по показам ряда):")
        out = []
        for c, r in t.items():
            nr = n.get(c, {"clicks": 0, "impressions": 0})
            out.append((c, int(r["clicks"]) - int(nr["clicks"]), int(r["impressions"]) - int(nr["impressions"])))
        for c, cc, ii in sorted(out, key=lambda x: -x[2])[:8]:
            print(f"  {c}: {cc}/{ii} {ctr(cc, ii)}  ({100 * ii / qi if qi else 0:.0f}% ряда)")

    print("видимый бренд по устройствам:")
    for r in q(site, ["device"], cf + [INC(region)]):
        print(f"  {r['keys'][0]}: {int(r['clicks'])}/{int(r['impressions'])} "
              f"{ctr(r['clicks'], r['impressions'])} поз.{r['position']:.1f}")


probe("KZ", "https://limestore.com/", "kz", "kaz")
for site, name in REGIONS["gcc"]["sites"].items():
    probe(name, site, "gcc", None)
