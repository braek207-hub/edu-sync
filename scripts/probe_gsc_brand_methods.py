# -*- coding: utf-8 -*-
"""Probe: каким методом собран «SEO Google» в ручной таблице Павла (KZ / UAE).

Неделя 33 (2026-08-10..16). Референсы из его листов: KZ=2101, UAE=2028 кликов.
Наш синк даёт KZ=1728, UAE(ОАЭ)=562 — ищем комбинацию фильтров GSC, дающую его цифры.
Read-only, в БД не пишет. Запуск: workflow probe-gsc-methods.yml (нужны Google-креды).
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

from sync.brand_terms import brand_regex
from sync.gsc import get_searchconsole_service

S, E = "2026-08-10", "2026-08-16"
service = get_searchconsole_service()


def q(site: str, filters: list[dict]) -> tuple[int, int]:
    body = {"startDate": S, "endDate": E, "dimensions": ["date"],
            "rowLimit": 25000, "type": "web"}
    if filters:
        body["dimensionFilterGroups"] = [{"filters": filters}]
    try:
        resp = service.searchanalytics().query(siteUrl=site, body=body).execute()
    except Exception as e:  # нет доступа/ресурса — печатаем и идём дальше
        print(f"    ! {site}: {e}")
        return (-1, -1)
    rows = resp.get("rows", [])
    return (sum(int(r.get("clicks", 0)) for r in rows),
            sum(int(r.get("impressions", 0)) for r in rows))


QF_KZ = {"dimension": "query", "operator": "includingRegex", "expression": brand_regex("kz")}
QF_GCC = {"dimension": "query", "operator": "includingRegex", "expression": brand_regex("gcc")}
QF_LIME = {"dimension": "query", "operator": "contains", "expression": "lime"}
CF = lambda c: {"dimension": "country", "operator": "equals", "expression": c}

print(f"неделя {S}..{E}; референсы Павла: KZ=2101, UAE=2028 кликов\n")

print("== KZ: limestore.com ==")
for label, filters in [
    ("наш синк: regex + country=kaz", [QF_KZ, CF("kaz")]),
    ("regex, БЕЗ страны", [QF_KZ]),
    ("contains 'lime' + country=kaz", [QF_LIME, CF("kaz")]),
    ("country=kaz, без query-фильтра (вкл. анонимные)", [CF("kaz")]),
]:
    c, i = q("https://limestore.com/", filters)
    print(f"  {label}: clicks={c}, imps={i}")

print("\n== UAE ==")
for site, label, filters in [
    ("https://ae.limestore.com/", "ae: наш синк (regex + country=are)", [QF_GCC, CF("are")]),
    ("https://ae.limestore.com/", "ae: regex, БЕЗ страны", [QF_GCC]),
    ("https://ae.limestore.com/", "ae: ВООБЩЕ без фильтров", []),
    ("https://ae.limestore.com/", "ae: country=are, без query (вкл. анонимные)", [CF("are")]),
    ("https://limestore.com/", "корень: regex + country=are", [QF_GCC, CF("are")]),
    ("https://limestore.com/", "корень: country=are, без query", [CF("are")]),
]:
    c, i = q(site, filters)
    print(f"  {label}: clicks={c}, imps={i}")
