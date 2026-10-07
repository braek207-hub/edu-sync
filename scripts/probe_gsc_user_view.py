# -*- coding: utf-8 -*-
"""Probe: брендовая выдача глазами пользователя страны.

1) Матрица страна пользователя × домен × класс запроса (бренд / аноним / небренд):
   показы, клики, средняя позиция. Позиция анонимных выводится из итога:
   pos_total·imp_total = Σ pos_класса·imp_класса.
2) Для стран KZ+GCC по видимому бренду, ключ = день × запрос × устройство:
   какие домены были в выдаче, кто стоял выше, куда ушли клики; уникальные поиски
   (сумма максимумов по ключу) против суммы показов доменов.
Период 07.09–04.10.2026. Read-only. Вывод — JSON-строки с префиксом JSON:."""
import json
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8")

from sync.brand_terms import brand_regex
from sync.gsc import get_searchconsole_service

S, E = "2026-09-07", "2026-10-04"
service = get_searchconsole_service()
HOSTS = {"root": "https://limestore.com/", "ae": "https://ae.limestore.com/",
         "sa": "https://sa.limestore.com/", "kw": "https://kw.limestore.com/",
         "qa": "https://qa.limestore.com/", "om": "https://om.limestore.com/",
         "bh": "https://bh.limestore.com/"}
TARGET = {"kaz": "root", "are": "ae", "sau": "sa", "kwt": "kw", "qat": "qa", "omn": "om", "bhr": "bh"}
RE = brand_regex("gcc")  # написания GCC ⊇ KZ (база + leem/арабские)
INC = {"dimension": "query", "operator": "includingRegex", "expression": RE}
EXC = {"dimension": "query", "operator": "excludingRegex", "expression": RE}


def q(site, dims, filters):
    out, start = [], 0
    while True:
        body = {"startDate": S, "endDate": E, "dimensions": dims, "rowLimit": 25000,
                "startRow": start, "type": "web"}
        if filters:
            body["dimensionFilterGroups"] = [{"filters": filters}]
        page = service.searchanalytics().query(siteUrl=site, body=body).execute().get("rows", [])
        out += page
        if len(page) < 25000:
            return out
        start += 25000


def agg(rows):
    return {r["keys"][0]: (int(r["clicks"]), int(r["impressions"]), float(r["position"])) for r in rows}


# 1) матрица
for host, site in HOSTS.items():
    tot, br, nb = agg(q(site, ["country"], [])), agg(q(site, ["country"], [INC])), agg(q(site, ["country"], [EXC]))
    for c in tot:
        tc, ti, tp = tot[c]
        bc, bi, bp = br.get(c, (0, 0, 0.0))
        nc, ni, np_ = nb.get(c, (0, 0, 0.0))
        ac, ai = tc - bc - nc, ti - bi - ni
        ap = (tp * ti - bp * bi - np_ * ni) / ai if ai > 0 else 0.0
        print("JSON:" + json.dumps({"t": "m", "host": host, "c": c, "tot": [tc, ti, round(tp, 2)],
                                    "br": [bc, bi, round(bp, 2)], "an": [ac, ai, round(ap, 2)],
                                    "nb": [nc, ni, round(np_, 2)]}, ensure_ascii=False))

# 2) ключи видимого бренда по странам KZ+GCC
for c, right in TARGET.items():
    keys = defaultdict(dict)  # key -> host -> (clicks, imp, pos)
    for host, site in HOSTS.items():
        for r in q(site, ["date", "query", "device"],
                   [{"dimension": "country", "operator": "equals", "expression": c}, INC]):
            keys[tuple(r["keys"])][host] = (int(r["clicks"]), int(r["impressions"]), float(r["position"]))
    s = {"t": "k", "c": c, "right": right, "sum_imp": 0, "uniq": 0,
         "only_right": [0, 0], "only_wrong": [0, 0], "both": [0, 0],  # [поиски, клики]
         "both_right_higher": 0, "both_wrong_higher": 0,
         "clicks_by_host": defaultdict(int), "imp_by_host": defaultdict(int),
         "wrong_hosts_in_only_wrong": defaultdict(int)}
    for hv in keys.values():
        u = max(v[1] for v in hv.values())
        s["sum_imp"] += sum(v[1] for v in hv.values())
        s["uniq"] += u
        clk = sum(v[0] for v in hv.values())
        for h, v in hv.items():
            s["clicks_by_host"][h] += v[0]
            s["imp_by_host"][h] += v[1]
        has_r = right in hv and hv[right][1] > 0
        wrong = {h: v for h, v in hv.items() if h != right and v[1] > 0}
        if has_r and not wrong:
            s["only_right"][0] += u; s["only_right"][1] += clk
        elif wrong and not has_r:
            s["only_wrong"][0] += u; s["only_wrong"][1] += clk
            for h, v in wrong.items():
                s["wrong_hosts_in_only_wrong"][h] += v[1]
        elif has_r and wrong:
            s["both"][0] += u; s["both"][1] += clk
            best_wrong = min(v[2] for v in wrong.values())
            if hv[right][2] <= best_wrong:
                s["both_right_higher"] += u
            else:
                s["both_wrong_higher"] += u
    print("JSON:" + json.dumps(s, ensure_ascii=False))
