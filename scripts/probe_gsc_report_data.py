# -*- coding: utf-8 -*-
"""Probe: данные отчёта «брендовая выдача LIME в Google по странам».

A) Совместные выдачи видимого бренда: ключ день × запрос × устройство у пользователя страны,
   набор наших доменов в ключе → поиски (max показов), клики и позиция по каждому домену.
B) Топ брендовых запросов страны: показы, клики, позиция по каждому домену.
C) Признаки анонимных для пары домен × своя страна: страницы входа (главная / каталог /
   товар / прочее), устройства, недели — у бренда, небренда и анонимных (= итог − оба).
Период 07.09–04.10.2026. Read-only. Вывод — строки B64:<base64 JSON>: лог Actions маскирует
строки секрета, а «{» и «}» среди них есть."""
import base64
import datetime as dt
import json
from collections import defaultdict
from urllib.parse import urlparse

from sync.brand_terms import brand_regex
from sync.gsc import get_searchconsole_service

S, E = "2026-09-07", "2026-10-04"
service = get_searchconsole_service()
HOSTS = {"root": "https://limestore.com/", "ae": "https://ae.limestore.com/",
         "sa": "https://sa.limestore.com/", "kw": "https://kw.limestore.com/",
         "qa": "https://qa.limestore.com/", "om": "https://om.limestore.com/",
         "bh": "https://bh.limestore.com/"}
OWN = {"kaz": "root", "are": "ae", "sau": "sa", "kwt": "kw", "qat": "qa", "omn": "om", "bhr": "bh"}
RE = brand_regex("gcc")
INC = {"dimension": "query", "operator": "includingRegex", "expression": RE}
EXC = {"dimension": "query", "operator": "excludingRegex", "expression": RE}
LOCALES = {"en", "ar", "ru", "kk", "kz", "en-ae", "ar-ae"}


def emit(obj):
    print("B64:" + base64.b64encode(json.dumps(obj, ensure_ascii=False).encode("utf-8")).decode("ascii"))


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


def cf(c):
    return {"dimension": "country", "operator": "equals", "expression": c}


def page_type(url):
    parts = [p for p in urlparse(url).path.split("/") if p]
    if parts and parts[0].lower() in LOCALES:
        parts = parts[1:]
    if not parts:
        return "home"
    if parts[0] in ("collections", "catalog", "category", "c"):
        return "catalog" if "products" not in parts else "product"
    if parts[0] in ("products", "product", "p"):
        return "product"
    return "other"


def week(d):
    x = dt.date.fromisoformat(d)
    return (x - dt.timedelta(days=x.weekday())).isoformat()


# A + B
for c in OWN:
    keys = defaultdict(dict)
    qagg = defaultdict(lambda: defaultdict(lambda: [0, 0, 0.0]))  # query -> host -> clk, imp, pos*imp
    for host, site in HOSTS.items():
        for r in q(site, ["date", "query", "device"], [cf(c), INC]):
            d, qu, dev = r["keys"]
            keys[(d, qu, dev)][host] = (int(r["clicks"]), int(r["impressions"]), float(r["position"]))
            a = qagg[qu][host]
            a[0] += int(r["clicks"]); a[1] += int(r["impressions"]); a[2] += float(r["position"]) * int(r["impressions"])
    combos = defaultdict(lambda: {"searches": 0, "hosts": defaultdict(lambda: [0, 0, 0.0, 0])})
    for hv in keys.values():
        shown = {h: v for h, v in hv.items() if v[1] > 0}
        if not shown:
            continue
        name = "+".join(sorted(shown, key=list(HOSTS).index))
        cb = combos[name]
        cb["searches"] += max(v[1] for v in shown.values())
        top = min(shown, key=lambda h: shown[h][2])
        for h, v in shown.items():
            a = cb["hosts"][h]
            a[0] += v[0]; a[1] += v[1]; a[2] += v[2] * v[1]; a[3] += max(v[1] for v in shown.values()) if h == top else 0
    emit({"t": "combo", "c": c, "combos": combos})
    tops = sorted(qagg.items(), key=lambda kv: -max(v[1] for v in kv[1].values()))[:15]
    emit({"t": "topq", "c": c, "rows": [[qu, hv] for qu, hv in tops]})

# C
for c, host in list(OWN.items()) + [("are", "root"), ("sau", "ae")]:
    site = HOSTS[host]
    out = {"t": "anon", "c": c, "host": host}
    for dim, fn in (("page", page_type), ("device", lambda x: x), ("date", week)):
        seg = {}
        for name, flt in (("tot", [cf(c)]), ("br", [cf(c), INC]), ("nb", [cf(c), EXC])):
            acc = defaultdict(lambda: [0, 0])
            for r in q(site, [dim], flt):
                k = fn(r["keys"][0])
                acc[k][0] += int(r["clicks"]); acc[k][1] += int(r["impressions"])
            seg[name] = acc
        out[dim] = {k: {"br": seg["br"].get(k, [0, 0]), "nb": seg["nb"].get(k, [0, 0]),
                        "an": [seg["tot"][k][0] - seg["br"].get(k, [0, 0])[0] - seg["nb"].get(k, [0, 0])[0],
                               seg["tot"][k][1] - seg["br"].get(k, [0, 0])[1] - seg["nb"].get(k, [0, 0])[1]]}
                    for k in seg["tot"]}
    emit(out)
