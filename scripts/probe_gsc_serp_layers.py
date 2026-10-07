# -*- coding: utf-8 -*-
"""Probe: совместные выдачи видимого бренда без перекоса от разных счётчиков показов.

Строка GSC «день × запрос × устройство» у пользователя страны сливает несколько выдач.
Если в ней ae. показан 10 раз, а kw. один раз, то kw. был лишь в одной из 10 выдач, а не во
всех. Строка раскладывается на слои: сайты по убыванию показов, сайт с меньшим числом показов
стоит в части выдач сайта с бОльшим (максимальное пересечение — то же допущение, что
«поисков = максимум показов»). Слой k = top-k сайтов, выдач в нём imp_k − imp_{k+1}.
Внутри слоя «кто выше» — по средней позиции сайта в строке. Клики сайта делятся по его слоям
пропорционально показам.

Для сравнения рядом считается старый способ (вся строка → набор всех её сайтов).
Период 07.09–04.10.2026. Read-only. Вывод — строки B64:<base64 JSON>."""
import base64
import json
from collections import defaultdict

from sync.brand_terms import brand_regex
from sync.gsc import get_searchconsole_service

S, E = "2026-09-07", "2026-10-04"
service = get_searchconsole_service()
HOSTS = {"root": "https://limestore.com/", "ae": "https://ae.limestore.com/",
         "sa": "https://sa.limestore.com/", "kw": "https://kw.limestore.com/",
         "qa": "https://qa.limestore.com/", "om": "https://om.limestore.com/",
         "bh": "https://bh.limestore.com/"}
ORDER = list(HOSTS)
OWN = {"kaz": "root", "are": "ae", "sau": "sa", "kwt": "kw", "qat": "qa", "omn": "om", "bhr": "bh"}
INC = {"dimension": "query", "operator": "includingRegex", "expression": brand_regex("gcc")}


OUT = open("layers.jsonl", "w", encoding="utf-8")  # артефакт: лог маскирует куски base64


def emit(obj):
    OUT.write(json.dumps(obj, ensure_ascii=False) + "\n")
    OUT.flush()
    print("B64:" + base64.b64encode(json.dumps(obj, ensure_ascii=False).encode("utf-8")).decode("ascii"))


def q(site, dims, filters):
    out, start = [], 0
    while True:
        body = {"startDate": S, "endDate": E, "dimensions": dims, "rowLimit": 25000,
                "startRow": start, "type": "web", "dimensionFilterGroups": [{"filters": filters}]}
        page = service.searchanalytics().query(siteUrl=site, body=body).execute().get("rows", [])
        out += page
        if len(page) < 25000:
            return out
        start += 25000


def new_combo():
    return {"searches": 0.0, "hosts": defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])}  # clk, imp, pos*imp, top


for c, own in OWN.items():
    keys = defaultdict(dict)
    for host, site in HOSTS.items():
        for r in q(site, ["date", "query", "device"],
                   [{"dimension": "country", "operator": "equals", "expression": c}, INC]):
            if int(r["impressions"]) > 0:
                keys[tuple(r["keys"])][host] = (int(r["clicks"]), int(r["impressions"]), float(r["position"]))

    combos = defaultdict(new_combo)
    cat = defaultdict(float)  # only_own / own_top / other_top / absent / only_other_single
    top_multi = defaultdict(float)  # сайт выше остальных наших в выдачах с 2+ сайтами
    with_own = defaultdict(float)  # выдачи, где рядом со своим стоит сайт h
    old = defaultdict(float)
    uniq = 0
    for hv in keys.values():
        ranked = sorted(hv, key=lambda h: (-hv[h][1], hv[h][2]))
        imps = [hv[h][1] for h in ranked] + [0]
        uniq += imps[0]
        for k in range(1, len(ranked) + 1):
            n = imps[k - 1] - imps[k]
            if n == 0:
                continue
            layer = ranked[:k]
            name = "+".join(sorted(layer, key=ORDER.index))
            top = min(layer, key=lambda h: (hv[h][2], h != own))
            cb = combos[name]
            cb["searches"] += n
            for h in layer:
                clk, imp, p = hv[h]
                a = cb["hosts"][h]
                a[0] += clk * n / imp; a[1] += n; a[2] += p * n; a[3] += n if h == top else 0
            if own in layer:
                for h in layer:
                    if h != own:
                        with_own[h] += n
                if k == 1:
                    cat["only_own"] += n
                elif top == own:
                    cat["own_top"] += n
                else:
                    cat["other_top"] += n
            else:
                cat["absent"] += n
            if k > 1:
                top_multi[top] += n
        # старый способ: вся строка целиком
        shown = list(hv)
        u = imps[0]
        if own in hv:
            if len(shown) == 1:
                old["only_own"] += u
            elif hv[own][2] <= min(hv[h][2] for h in shown if h != own):
                old["own_top"] += u
            else:
                old["other_top"] += u
        else:
            old["absent"] += u
    emit({"t": "layers", "c": c, "own": own, "uniq": uniq, "cat": cat, "old": old,
          "top_multi": top_multi, "with_own": with_own, "combos": combos})
