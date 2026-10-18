# -*- coding: utf-8 -*-
"""Пользовательские правила модулей: проверка, нормализация и сопоставление со строкой.

Чистый модуль, без сети и базы: им пользуются и прогон, и пробный прогон — один
путь кода, иначе превью обещало бы не то, что потом сделает робот.

Значения строк — нижний регистр и ё=е, как и у сравниваемых имён: Директ ё и е
не различает, и правило «учёба» обязано ловить запрос «учеба».
"""

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Tuple, Union

from sync.agent.writer import negatives
from sync.modules.text import word_list, yo
from sync.placements import classify

MAX_RULES = 30
MAX_CONDITIONS = 6
NAME_MAX = 80
_ID = re.compile(r"^[a-z0-9]{6}$")

PROTECT = "protect"
ACTIONS: Dict[str, Tuple[str, ...]] = {
    "placements": ("block", "propose", PROTECT),
    "queries": ("minus_word", "minus_phrase", "propose_word", "propose_phrase", PROTECT),
}
# Минус-слово берётся из условия «содержит слово»: без него ставить нечего.
WORD_ACTIONS = frozenset({"minus_word", "propose_word"})
WINDOWS: Tuple[int, ...] = (0, 7, 30)
WHERE: Tuple[str, ...] = ("seen", "account")
CATEGORIES: Tuple[str, ...] = ("app", "dsp", "game", "junk", "site")
# У queries where нет: запрос всегда судится в своей кампании.
_SCOPE_OF_WHERE = {"seen": "campaign", "account": "account", None: "campaign"}

_RANGE = ("gte", "lte")
_COUNT = ("gte", "lte", "eq")
_STATS = {"clicks": _COUNT, "cost": _RANGE, "ctr": _RANGE, "leads": _COUNT,
          "cr": _RANGE, "cr_x": _RANGE, "cpl": _RANGE, "cpl_x": ("gte",)}
FIELDS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "placements": {"name": ("contains", "starts", "ends", "equals"),
                   "category": ("is", "is_not"), "impressions": _RANGE, **_STATS},
    "queries": {"query": ("has_word", "has_phrase", "equals"), "words": _RANGE, **_STATS},
}
TEXT_FIELDS = frozenset({"name", "category", "query"})
LEAD_FIELDS = frozenset({"leads", "cr", "cr_x", "cpl", "cpl_x"})
MONEY_FIELDS = frozenset({"cost", "cpl", "cpl_x"})
# Доля и цена без базы не определены: 0 лидов — это не «цена лида 0 ₽», а цены нет.
_BASE = {"ctr": "impressions", "cr": "clicks", "cr_x": "clicks", "cpl": "leads", "cpl_x": "leads"}


@dataclass(frozen=True)
class Cond:
    field: str
    op: str
    value: Union[str, int, float]


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    action: str
    window: Optional[int]  # у queries окно одно на модуль (window_days) и у правила не хранится
    where: Optional[str]   # только placements
    only: FrozenSet[int]
    except_: FrozenSet[int]
    conditions: Tuple[Cond, ...]


@dataclass
class Row:
    """Строка для правил: площадка или запрос × кампания (scope="campaign")
    или площадка по логину целиком (scope="account", для where=account).

    metrics: clicks, cost (₽ с НДС), impressions, ctr и cr (в процентах), leads, cpl.
    account: cr и cpl кабинета за то же окно. ctr, cr, cpl и их «_x» без базы
    (показов, кликов, лидов > 0) движок считает неопределёнными сам, что бы ни
    пришло в metrics. leads_known=False — лиды нельзя посчитать (нет целей-лидов,
    цель не видна кампании); money_known=False — кабинет не в рублях.
    """
    object: str
    campaign_id: int
    category: Optional[str]
    metrics: Dict[str, Optional[float]]
    account: Dict[str, Optional[float]]
    leads_known: bool
    money_known: bool
    scope: str


@dataclass
class Match:
    """Итог правил по строке — одна из трёх форм:

    1. rule задан, held_by=None — действует rule (сработавший protect или первое
       сработавшее по списку);
    2. rule=None, held_by=<id> — защиту held_by проверить нельзя, строка держится:
       ни словарь, ни спам, ни правила по ней не действуют, их находки — unverified
       от имени held_by;
    3. rule=None, held_by=None, matched=[] — ничего не сработало, но часть правил
       проверить нельзя.

    matched — все сработавшие в порядке списка; unverified — id правил, чьи
    собственные условия не проверить.
    """
    rule: Optional[Rule]
    matched: List[dict]
    unverified: List[str]
    held_by: Optional[str]


class _Invalid(ValueError):
    pass


def _is_num(value: Any) -> bool:
    # bool — подкласс int, но «клики ≥ True» правилом не является.
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _norm(field: str, text: Any) -> str:
    if field == "name":
        return yo(classify.normalize(text))
    if field == "query":
        return yo(negatives.normalize_phrase(text))
    return yo(str(text or "").strip().lower())


def _campaigns(raw: Any) -> Tuple[FrozenSet[int], FrozenSet[int]]:
    if raw is None:
        return frozenset(), frozenset()
    if not isinstance(raw, dict):
        raise _Invalid("campaigns не объект")
    out = []
    for key in ("only", "except"):
        ids = raw.get(key) or []
        if not isinstance(ids, list):
            raise _Invalid("campaigns.%s не список" % key)
        good = set()
        for x in ids:
            # Id кампаний бывают и строкой: в JS они не помещаются в безопасное целое.
            if isinstance(x, str) and x.strip().isdigit():
                x = int(x.strip())
            if not _is_int(x) or x <= 0:
                raise _Invalid("campaigns.%s: «%s» не Id кампании" % (key, x))
            good.add(x)
        out.append(frozenset(good))
    return out[0], out[1]


def _cond(module_key: str, raw: Any) -> Cond:
    if not isinstance(raw, dict):
        raise _Invalid("условие не объект")
    field, op, value = raw.get("field"), raw.get("op"), raw.get("value")
    ops = FIELDS[module_key].get(field) if isinstance(field, str) else None
    if ops is None:
        raise _Invalid("поле «%s» не для модуля %s" % (field, module_key))
    if op not in ops:
        raise _Invalid("оператор «%s» не для поля %s" % (op, field))
    if field in TEXT_FIELDS:
        if not isinstance(value, str) or not _norm(field, value):
            raise _Invalid("%s: пустое или нестроковое значение" % field)
        value = _norm(field, value)
        if field == "category" and value not in CATEGORIES:
            raise _Invalid("category: «%s» не категория словаря" % value)
        # Слово сравнивается точно: из двух слов или со знаками оно не совпало бы
        # ни с одним запросом, а minus_word поставил бы мусор. Слово через дефис
        # Директ тоже делит на два: «санкт-петербург» минус-словом не одно слово.
        if op == "has_word" and word_list(value) != [value]:
            raise _Invalid("has_word: «%s» не одно слово" % value)
        if op == "has_phrase" and not word_list(value):
            raise _Invalid("has_phrase: во фразе нет слов")
        return Cond(field, op, value)
    if not _is_num(value) or value < 0:
        raise _Invalid("%s: значение «%s» не неотрицательное число" % (field, value))
    if field == "words" and not _is_int(value):
        raise _Invalid("words: число слов не целое")
    return Cond(field, op, value)


def _compile(module_key: str, item: Any) -> Rule:
    if not isinstance(item, dict):
        raise _Invalid("правило не объект")
    rule_id = item.get("id")
    if not isinstance(rule_id, str) or not _ID.match(rule_id):
        raise _Invalid("id не из 6 символов [a-z0-9]")
    name = item.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= NAME_MAX:
        raise _Invalid("name не строка 1–%d символов" % NAME_MAX)
    if not isinstance(item.get("enabled", True), bool):
        raise _Invalid("enabled не true/false")
    action = item.get("action")
    if action not in ACTIONS[module_key]:
        raise _Invalid("действие «%s» не для модуля %s" % (action, module_key))
    window: Optional[int] = None
    where: Optional[str] = None
    if module_key == "placements":
        window = item.get("window")
        if not _is_int(window) or window not in WINDOWS:
            raise _Invalid("окно «%s» не из %s" % (window, WINDOWS))
        where = item.get("where", "seen")
        if where not in WHERE:
            raise _Invalid("where «%s» не из %s" % (where, WHERE))
    only, except_ = _campaigns(item.get("campaigns"))
    raw = item.get("conditions")
    # Правило без условий совпало бы с каждой строкой кабинета.
    if not isinstance(raw, list) or not raw:
        raise _Invalid("нет условий")
    if len(raw) > MAX_CONDITIONS:
        raise _Invalid("условий %d при пределе %d" % (len(raw), MAX_CONDITIONS))
    conds = tuple(_cond(module_key, c) for c in raw)
    if window == 0 and any(c.field in LEAD_FIELDS for c in conds):
        raise _Invalid("условия по лидам требуют окна 7 или 30: в дневном отчёте нет целей")
    if action in WORD_ACTIONS and not any(c.op == "has_word" for c in conds):
        raise _Invalid("%s без условия query has_word" % action)
    return Rule(rule_id, name.strip(), action, window, where, only, except_, conds)


def compile_rules(module_key: str, raw: list) -> Tuple[List[Rule], List[dict]]:
    """Включённые валидные правила в порядке списка + заметки об отброшенных.

    Невалидное правило отбрасывается целиком, а не чинится: правило, понятое
    иначе, чем его написал человек, закрывало бы не то.
    """
    rules: List[Rule] = []
    notes: List[dict] = []
    items = raw if isinstance(raw, list) else []
    # Id входит в fingerprint находки: какое из двух правил с одним id владеет её
    # историей, не решить, поэтому отбрасываются оба — и при невалидном первом.
    counts = Counter(i.get("id") for i in items
                     if isinstance(i, dict) and isinstance(i.get("id"), str))
    for pos, item in enumerate(items):
        if isinstance(item, dict) and item.get("enabled", True) is False:
            continue
        rule_id = item.get("id") if isinstance(item, dict) else None
        try:
            if pos >= MAX_RULES:
                raise _Invalid("правило №%d сверх предела %d" % (pos + 1, MAX_RULES))
            if isinstance(rule_id, str) and counts[rule_id] > 1:
                raise _Invalid("id «%s» повторяется" % rule_id)
            rule = _compile(module_key, item)
        except _Invalid as e:
            notes.append({"rule_id": rule_id if isinstance(rule_id, str) else None,
                          "code": "rule_invalid", "detail": str(e)})
            continue
        rules.append(rule)
    return rules, notes


def _ratio(a: Optional[float], b: Optional[float]) -> Optional[float]:
    # Кабинет без конверсий или без цены лида — сравнивать не с чем: условие ложно,
    # а не «не проверено», данные-то есть.
    if a is None or not b:
        return None
    return a / b


def _has_seq(words: List[str], part: List[str]) -> bool:
    n = len(part)
    return any(words[i:i + n] == part for i in range(len(words) - n + 1))


def _check(cond: Cond, row: Row, name: str, query: str) -> Tuple[Optional[bool], Any]:
    """(True/False, None — не проверить; фактическое значение для details.matched)."""
    f, op, v = cond.field, cond.op, cond.value
    if (f in LEAD_FIELDS and not row.leads_known) or (f in MONEY_FIELDS and not row.money_known):
        return None, None
    if f == "name":
        ok = {"contains": v in name, "starts": name.startswith(v),
              "ends": name.endswith(v), "equals": name == v}[op]
        return ok, name
    if f == "category":
        # Без вердикта словаря и «не равно» ложно: ложное совпадение только отказывает.
        if row.category is None:
            return False, None
        return (row.category == v) == (op == "is"), row.category
    if f == "query":
        if op == "equals":
            return query == v, query
        words = word_list(query)
        if op == "has_word":
            return v in words, query
        return _has_seq(words, word_list(v)), query
    base = _BASE.get(f)
    if base is not None and not (row.metrics.get(base) or 0) > 0:
        return False, None
    if f == "words":
        actual: Optional[float] = len(word_list(query))
    elif f == "cr_x":
        actual = _ratio(row.metrics.get("cr"), row.account.get("cr"))
    elif f == "cpl_x":
        actual = _ratio(row.metrics.get("cpl"), row.account.get("cpl"))
    else:
        actual = row.metrics.get(f)
    if actual is None:
        return False, None
    # Сравнивается то, что человек увидит в журнале: 9,996 показано как 10 и должно
    # пройти «≥ 10», иначе запись «10 ≥ 10 — не сработало» не объяснить.
    actual = round(float(actual), 2)
    if op == "gte":
        return actual >= v, actual
    if op == "lte":
        return actual <= v, actual
    return actual == round(float(v), 2), actual


def _judge(rule: Rule, row: Row, name: str, query: str) -> Tuple[Optional[bool], List[dict]]:
    conds: List[dict] = []
    unknown = False
    for c in rule.conditions:
        ok, actual = _check(c, row, name, query)
        # Ложное условие решает исход и без непроверяемых: правило не сработает при любых лидах.
        if ok is False:
            return False, []
        unknown = unknown or ok is None
        conds.append({"field": c.field, "op": c.op, "value": c.value, "actual": actual})
    return (None if unknown else True), conds


def _applies(rule: Rule, row: Row) -> bool:
    # Защита действует на обоих уровнях: площадка, защищённая в кампании, не должна
    # уйти и под запрет по логину. Лишняя защита только отказывает.
    if rule.action != PROTECT and _SCOPE_OF_WHERE[rule.where] != row.scope:
        return False
    if row.scope == "account":
        # Строка — площадка по логину целиком: кампании для записи отбирает прогон.
        return True
    if rule.only and row.campaign_id not in rule.only:
        return False
    return row.campaign_id not in rule.except_


def evaluate(rules: List[Rule], row: Row) -> Optional[Match]:
    """Правила против строки. None — ни одно не сработало и все проверены.

    Правила берутся только своего уровня (where ↔ row.scope), protect — любого.
    Действует первое сработавшее по списку, но protect сильнее любого правила
    независимо от места. Непроверенная защита без сработавшей держит строку
    (held_by, форма 2 в Match): закрыть, не зная, защищена ли площадка, нельзя.
    """
    matched: List[Tuple[Rule, List[dict]]] = []
    unverified: List[str] = []
    held_by: Optional[str] = None
    name, query = _norm("name", row.object), _norm("query", row.object)
    for rule in rules:
        if not _applies(rule, row):
            continue
        state, conds = _judge(rule, row, name, query)
        if state is None:
            unverified.append(rule.id)
            if rule.action == PROTECT and held_by is None:
                held_by = rule.id
        elif state:
            matched.append((rule, conds))
    if not matched and not unverified:
        return None
    first = next((r for r, _ in matched if r.action == PROTECT), None)
    if first is not None:
        # Сработавшая защита решает и без непроверенной.
        held_by = None
    elif held_by is None and matched:
        first = matched[0][0]
    return Match(rule=first,
                 matched=[{"id": r.id, "name": r.name, "conds": c} for r, c in matched],
                 unverified=unverified, held_by=held_by)


def snapshot(rule: Rule) -> dict:
    """Снимок правила для details.rule: по нему Panda рисует, почему и что сделано."""
    return {"id": rule.id, "name": rule.name, "action": rule.action,
            "window": rule.window, "where": rule.where,
            "conditions": [{"field": c.field, "op": c.op, "value": c.value}
                           for c in rule.conditions]}
