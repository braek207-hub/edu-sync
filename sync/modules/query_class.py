# -*- coding: utf-8 -*-
"""Класс поискового запроса и решение набора для «Чистильщика запросов» (режим «Авто»).

Зеркало lib/queries/classify.ts и lib/queries/sets.ts в Panda-BI: страница считает предложения
в TS, робот в режиме «Авто» — здесь, и предложения обязаны совпадать. Паритет держит общий
файл векторов (Panda lib/queries/vectors.json, копия tests/data/query_vectors.json): правка
алгоритма — в обоих репо вместе с векторами.
"""

import re
from typing import Dict, List, Optional, Sequence, Tuple

COMMON_TRASH = ["вакансии", "вакансия", "работа в", "авито", "avito", "бесплатно", "скачать",
                "своими руками", "выкройка", "секонд", "б/у"]
MAX_PHRASE_WORDS = 7
PREFIX_MIN = 4
BRAND_PREFIX_MIN = 3
NEAR_MIN = 4
CLEAN_CLASSES = ("brand", "nonbrand", "comp", "trash", "rules")
MANUAL_NONE = "none"

_SPLIT = re.compile(r"[^a-zа-я0-9&é]+")
_DIGITS = re.compile(r"^[0-9 ]{5,}$")


def normalize(s: str) -> str:
    return (s or "").lower().replace("ё", "е").strip()


def words(s: str) -> List[str]:
    return [w for w in _SPLIT.split(normalize(s)) if w]


def _token_hit(token: str, entry: str, prefix_min: int) -> bool:
    return token == entry or (len(entry) >= prefix_min and token.startswith(entry))


def phrase_in(qw: Sequence[str], entry: str, prefix_min: int = PREFIX_MIN) -> bool:
    ew = words(entry)
    if not ew or len(ew) > len(qw):
        return False
    for i in range(len(qw) - len(ew) + 1):
        if all(_token_hit(qw[i + j], ew[j], prefix_min) if j == len(ew) - 1
               else qw[i + j] == ew[j] for j in range(len(ew))):
            return True
    return False


def distance(a: str, b: str) -> int:
    m, n = len(a), len(b)
    if abs(m - n) > 2:
        return 9
    d = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m + 1):
        d[i][0] = i
    for j in range(n + 1):
        d[0][j] = j
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            c = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + c)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[m][n]


def _near_limit(token: str, entry: str) -> int:
    return 1 if len(token) <= 3 or len(entry) <= 5 else 2


def _near_brand(token: str, core: List[str], not_brand: set) -> bool:
    if len(token) < 3 or token in not_brand:
        return False
    return any(distance(token, e) <= _near_limit(token, e)
               or (len(token) > len(e) + 1 and distance(token[:len(e)], e) <= 1)
               for e in core)


def classify(query: str, d: Dict[str, List[str]]) -> Tuple[str, Optional[str]]:
    qw = words(query)
    q = " ".join(qw)
    for e in d.get("trash", []):
        if phrase_in(qw, e):
            return "trash", normalize(e)
    if _DIGITS.match(q):
        return "trash", q
    not_brand = {normalize(x) for x in d.get("notBrand", [])}
    for e in d.get("brand", []):
        en = normalize(e)
        if len(words(en)) > 1:
            if phrase_in(qw, en, BRAND_PREFIX_MIN):
                return "brand", en
            continue
        if any(t not in not_brand and _token_hit(t, en, BRAND_PREFIX_MIN) for t in qw):
            return "brand", en
    for e in d.get("comp", []):
        if phrase_in(qw, e):
            return "comp", normalize(e)
    core = [e for e in (normalize(x) for x in d.get("brand", []))
            if len(e) >= NEAR_MIN and " " not in e]
    if core:
        for t in qw:
            if _near_brand(t, core, not_brand):
                return "near", t
        for i in range(1, len(qw)):
            glued = qw[i - 1] + qw[i]
            if len(glued) <= 6 and _near_brand(glued, core, not_brand):
                return "near", glued
    return "nonbrand", None


def is_protected(query: str, never: Sequence[str]) -> bool:
    qw = words(query)
    return any(phrase_in(qw, e) for e in never)


def query_phrase(query: str) -> str:
    return " ".join(words(query)[:MAX_PHRASE_WORDS])


def set_for(campaign: dict, sets: List[dict], manual: Dict[str, str]
            ) -> Tuple[Optional[dict], str]:
    """(набор или None, причина): manual-none / manual / added / name / none."""
    m = manual.get(str(campaign["id"]))
    if m == MANUAL_NONE:
        return None, "manual-none"
    if m:
        for s in sets:
            if s["id"] == m:
                return s, "manual"
    for s in sets:
        if int(campaign["id"]) in [int(x) for x in s.get("campaigns") or []]:
            return s, "added"
    name = normalize(campaign.get("name") or "")
    for s in sets:
        inc, exc = normalize(s.get("include") or ""), normalize(s.get("exclude") or "")
        if inc and inc in name and not (exc and exc in name):
            return s, "name"
    return None, "none"


def judge(row: dict, s: Optional[dict], d: Dict[str, List[str]], th: Dict[str, float]
          ) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
    """(класс, найденное, класс чистки или None, минус или None)."""
    cls, hit = classify(row["query"], d)
    none = (cls, hit, None, None)
    if not s or not s.get("on") or cls == "near" or is_protected(row["query"], d.get("never", [])):
        return none
    clean = s.get("clean") or []
    if cls != "nonbrand":
        return (cls, hit, cls, hit) if cls in clean else none
    if "nonbrand" in clean:
        return cls, hit, "nonbrand", query_phrase(row["query"])
    conv = row.get("conversions")
    if "rules" in clean and conv is not None:
        bad = row["cost"] >= th["noConvCost"] if conv == 0 else row["cost"] / conv > th["maxCpa"]
        if bad:
            return cls, hit, "rules", query_phrase(row["query"])
    return none
