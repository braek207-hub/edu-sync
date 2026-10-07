# -*- coding: utf-8 -*-
"""Probe: раскладка показов/кликов GSC LIME (KZ, GCC) и кросс-показы между витринами.

1. Доступные ресурсы (есть ли доменный sc-domain:limestore.com — он дедуплицирует хосты).
2. По каждой витрине: видимый бренд / анонимные / видимый небренд × своя страна / чужие.
3. Кросс-показы: сумма показов по хостам против доменного ресурса (byProperty) по странам;
   плюс оценка по ключам (дата×запрос×страна×устройство), встречающимся на 2+ хостах.
Период 07.09–04.10.2026. Read-only."""
import sys
from collections import defaultdict
from itertools import combinations

sys.stdout.reconfigure(encoding="utf-8")

from sync.brand_terms import brand_regex
from sync.gsc import get_searchconsole_service

S, E = "2026-09-07", "2026-10-04"
service = get_searchconsole_service()

HOSTS = {  # ресурс → (регион терминов, страна витрины)
    "https://limestore.com/": ("kz", "kaz"),
    "https://ae.limestore.com/": ("gcc", "are"),
    "https://sa.limestore.com/": ("gcc", "sau"),
    "https://kw.limestore.com/": ("gcc", "kwt"),
    "https://qa.limestore.com/": ("gcc", "qat"),
    "https://bh.limestore.com/": ("gcc", "bhr"),
    "https://om.limestore.com/": ("gcc", "omn"),
}
COUNTRIES = ["kaz", "are", "sau", "kwt", "qat", "bhr", "omn"]
INC = lambda reg: {"dimension": "query", "operator": "includingRegex", "expression": brand_regex(reg)}
EXC = lambda reg: {"dimension": "query", "operator": "excludingRegex", "expression": brand_regex(reg)}


def q(site, dims, filters=None, agg=None):
    body = {"startDate": S, "endDate": E, "dimensions": dims, "rowLimit": 25000, "type": "web"}
    if filters:
        body["dimensionFilterGroups"] = [{"filters": filters}]
    if agg:
        body["aggregationType"] = agg
    out, start = [], 0
    while True:
        body["startRow"] = start
        rows = service.searchanalytics().query(siteUrl=site, body=body).execute().get("rows", [])
        out += rows
        if len(rows) < 25000:
            return out
        start += 25000


def by_country(rows):
    return {r["keys"][0]: (int(r["clicks"]), int(r["impressions"])) for r in rows}


def ctr(c, i):
    return f"{100 * c / i:.1f}%" if i else "—"


print("=== 1. Ресурсы сервис-аккаунта")
sites = service.sites().list().execute().get("siteEntry", [])
for s in sites:
    print(f"  {s['siteUrl']}  {s['permissionLevel']}")
domain = next((s["siteUrl"] for s in sites if s["siteUrl"] == "sc-domain:limestore.com"), None)

print("\n=== 2. Раскладка по витринам (клики/показы CTR)")
print("сайт | страна | видимый бренд | анонимные | видимый небренд | итого")
host_country = {}  # (host, страна) → total (c, i), бренд+аноним (c, i)
for site, (reg, home) in HOSTS.items():
    t = by_country(q(site, ["country"]))
    b = by_country(q(site, ["country"], [INC(reg)]))
    n = by_country(q(site, ["country"], [EXC(reg)]))
    acc = {"своя": [0] * 6, "чужие": [0] * 6}
    for c, (tc, ti) in t.items():
        bc, bi = b.get(c, (0, 0))
        nc, ni = n.get(c, (0, 0))
        k = "своя" if c == home else "чужие"
        for j, v in enumerate((bc, bi, tc - bc - nc, ti - bi - ni, nc, ni)):
            acc[k][j] += v
        host_country[(site, c)] = ((tc, ti), (tc - nc, ti - ni))
    for k, a in acc.items():
        tc, ti = a[0] + a[2] + a[4], a[1] + a[3] + a[5]
        print(f"{site.split('//')[1].rstrip('/'):18s} | {k:5s} | {a[0]}/{a[1]} {ctr(a[0], a[1])} | "
              f"{a[2]}/{a[3]} {ctr(a[2], a[3])} | {a[4]}/{a[5]} {ctr(a[4], a[5])} | {tc}/{ti} {ctr(tc, ti)}")

print("\n=== 3a. Кросс-показы: сумма хостов против доменного ресурса (по стране пользователя)")
if domain:
    for label, filt in [("всё", None), ("бренд+аноним", "nb")]:
        print(f"-- {label}")
        if filt:
            tot_d = by_country(q(domain, ["country"], agg="byProperty"))
            nb_d = by_country(q(domain, ["country"], [EXC("gcc")], agg="byProperty"))
            dom = {c: (tot_d[c][0] - nb_d.get(c, (0, 0))[0], tot_d[c][1] - nb_d.get(c, (0, 0))[1]) for c in tot_d}
        else:
            dom = by_country(q(domain, ["country"], agg="byProperty"))
        for c in COUNTRIES:
            idx = 1 if filt else 0
            hs = sum(v[idx][1] for (h, cc), v in host_country.items() if cc == c)
            hc = sum(v[idx][0] for (h, cc), v in host_country.items() if cc == c)
            dc, di = dom.get(c, (0, 0))
            dup = hs - di
            print(f"  {c}: сумма хостов {hc}/{hs} | домен {dc}/{di} | дубли показов {dup} "
                  f"({100 * dup / hs if hs else 0:.0f}% суммы) | клики хостов−домен {hc - dc}")
    all_h = sum(v[0][1] for v in host_country.values())
    all_d = sum(v[1] for v in by_country(q(domain, ["country"], agg="byProperty")).values())
    print(f"  ВСЕ СТРАНЫ: сумма 7 хостов {all_h} | домен (все хосты домена) {all_d}")
else:
    print("  доменного ресурса нет — только оценка 3b")

print("\n=== 3b. Ключи дата×запрос×страна×устройство (видимый бренд) на 2+ хостах")
keys = defaultdict(dict)  # key → {host: (c, i, pos)}
for site, (reg, _) in HOSTS.items():
    for r in q(site, ["date", "query", "country", "device"], [INC("gcc")]):
        keys[tuple(r["keys"])][site] = (int(r["clicks"]), int(r["impressions"]), r["position"])
for scope, cset in [("страны GCC", {"are", "sau", "kwt", "qat", "bhr", "omn"}), ("Казахстан", {"kaz"}),
                    ("прочие страны", None)]:
    sel = {k: v for k, v in keys.items()
           if (k[2] in cset if cset else k[2] not in COUNTRIES)}
    tot_i = sum(x[1] for v in sel.values() for x in v.values())
    multi = {k: v for k, v in sel.items() if len(v) >= 2}
    m_i = sum(x[1] for v in multi.values() for x in v.values())
    upper = sum(sum(x[1] for x in v.values()) - max(x[1] for x in v.values()) for v in multi.values())
    print(f"-- {scope}: показов видимого бренда {tot_i}; на ключах с 2+ хостами {m_i} "
          f"({100 * m_i / tot_i if tot_i else 0:.0f}%); верхняя граница дублей {upper} "
          f"({100 * upper / tot_i if tot_i else 0:.0f}%)")
    pairs = defaultdict(lambda: [0, 0])
    for v in multi.values():
        for a, b in combinations(sorted(v), 2):
            pairs[(a, b)][0] += min(v[a][1], v[b][1]); pairs[(a, b)][1] += 1
    for (a, b), (mi, n) in sorted(pairs.items(), key=lambda x: -x[1][0])[:6]:
        print(f"   {a.split('//')[1][:6]}+{b.split('//')[1][:6]}: пересечение ≥{mi} показов, ключей {n}")
    # Кто первый и кто получает клики на мульти-ключах
    first = defaultdict(lambda: [0, 0, 0])
    for v in multi.values():
        top = min(v, key=lambda h: v[h][2])
        for h, (c, i, p) in v.items():
            f = first[h]
            f[0] += i; f[1] += c; f[2] += i if h == top else 0
    for h, (i, c, ti) in sorted(first.items(), key=lambda x: -x[1][0])[:7]:
        print(f"   {h.split('//')[1][:16]:16s} на мульти-ключах {c}/{i} {ctr(c, i)}; выше всех в {100 * ti / i if i else 0:.0f}% показов")
