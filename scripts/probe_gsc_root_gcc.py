# -*- coding: utf-8 -*-
"""Probe: корневой limestore.com у пользователей стран GCC + совместные показы с витриной страны.

По каждой стране c: корень (бренд/аноним/небренд), витрина страны (то же), и по ключам
дата×запрос×устройство видимого бренда: показы витрины, корня, сумма и минимум уникальных
поисков (сумма максимумов). Период 07.09–04.10.2026. Read-only."""
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8")

from sync.brand_terms import brand_regex
from sync.gsc import get_searchconsole_service

S, E = "2026-09-07", "2026-10-04"
service = get_searchconsole_service()
ROOT = "https://limestore.com/"
HOME = {"are": "https://ae.limestore.com/", "sau": "https://sa.limestore.com/",
        "kwt": "https://kw.limestore.com/", "qat": "https://qa.limestore.com/",
        "omn": "https://om.limestore.com/", "bhr": "https://bh.limestore.com/"}
INC = {"dimension": "query", "operator": "includingRegex", "expression": brand_regex("gcc")}
EXC = {"dimension": "query", "operator": "excludingRegex", "expression": brand_regex("gcc")}
CF = lambda c: {"dimension": "country", "operator": "equals", "expression": c}


def q(site, dims, filters):
    body = {"startDate": S, "endDate": E, "dimensions": dims, "rowLimit": 25000, "type": "web",
            "dimensionFilterGroups": [{"filters": filters}]}
    return service.searchanalytics().query(siteUrl=site, body=body).execute().get("rows", [])


def tot(rows):
    return sum(int(r["clicks"]) for r in rows), sum(int(r["impressions"]) for r in rows)


def split(site, c):
    t, b, n = tot(q(site, ["date"], [CF(c)])), tot(q(site, ["date"], [CF(c), INC])), tot(q(site, ["date"], [CF(c), EXC]))
    return b, (t[0] - b[0] - n[0], t[1] - b[1] - n[1]), n


def f(x):
    c, i = x
    return f"{c}/{i} {100 * c / i:.1f}%" if i else f"{c}/{i} —"


for c, home in HOME.items():
    rb, ra, rn = split(ROOT, c)
    hb, ha, hn = split(home, c)
    print(f"\n=== {c}")
    print(f"  витрина {home.split('//')[1][:2]}: бренд {f(hb)} | аноним {f(ha)} | небренд {f(hn)}")
    print(f"  корень          : бренд {f(rb)} | аноним {f(ra)} | небренд {f(rn)}")
    keys = defaultdict(lambda: [(0, 0, 0.0), (0, 0, 0.0)])
    for j, site in enumerate((home, ROOT)):
        for r in q(site, ["date", "query", "device"], [CF(c), INC]):
            keys[tuple(r["keys"])][j] = (int(r["clicks"]), int(r["impressions"]), r["position"])
    hi = sum(v[0][1] for v in keys.values()); ri = sum(v[1][1] for v in keys.values())
    both = [v for v in keys.values() if v[0][1] and v[1][1]]
    b_sum = sum(v[0][1] + v[1][1] for v in both); b_max = sum(max(v[0][1], v[1][1]) for v in both)
    only_root = [v for v in keys.values() if v[1][1] and not v[0][1]]
    print(f"  видимый бренд по ключам: витрина {hi}, корень {ri}, сумма {hi + ri}, "
          f"уникальных поисков не меньше {hi + ri - (b_sum - b_max)} (дубли ≤ {b_sum - b_max}, "
          f"{100 * (b_sum - b_max) / (hi + ri) if hi + ri else 0:.0f}% суммы)")
    print(f"  корень без витрины: {sum(v[1][1] for v in only_root)} показов, {sum(v[1][0] for v in only_root)} кликов")
    if both:
        hp = sum(v[0][2] * v[0][1] for v in both) / sum(v[0][1] for v in both)
        rp = sum(v[1][2] * v[1][1] for v in both) / sum(v[1][1] for v in both)
        hc = sum(v[0][0] for v in both); rc = sum(v[1][0] for v in both)
        print(f"  на общих ключах: ср.поз. витрины {hp:.1f}, корня {rp:.1f}; клики витрина {hc}, корень {rc}")
