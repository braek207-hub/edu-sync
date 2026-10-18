# -*- coding: utf-8 -*-
"""Цели-лиды и строки для пользовательских правил.

Чистый модуль: отчёт (direct.placements_window или queries.rule_raw) уже получен,
здесь только счёт. Один путь для обоих модулей и для пробного прогона — иначе
превью и робот по-разному посчитали бы одну и ту же цену лида.
"""

from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from sync.modules.rules import Row

# Потолок Reports API на число целей в одном отчёте: больше целей-лидов в один отчёт не
# попадёт, а отрезанная цель дала бы «ноль лидов» там, где их просто не спросили.
MAX_LEAD_GOALS = 10
BY = ("campaign", "account")
# campaign_id строки по логину целиком (scope="account"): своей кампании у неё нет,
# кампании для записи отбирает прогон.
ACCOUNT_CAMPAIGN_ID = 0


def _ids(raw: Any) -> List[int]:
    out = set()
    for g in raw if isinstance(raw, (list, tuple)) else []:
        if isinstance(g, bool):
            continue
        if isinstance(g, int):
            gid = g
        elif isinstance(g, str) and g.strip().isdigit():
            gid = int(g.strip())
        else:
            continue
        if gid > 0:
            out.add(gid)
    return sorted(out)


def lead_goals(settings: dict, project_goals: List[int]) -> Tuple[List[int], Optional[str]]:
    """(цели-лиды, код причины или None).

    Цепочка: lead_goal_ids модуля, иначе цели-лиды проекта (store.project_lead_goals).
    Настройка модуля сильнее проекта целиком, не дополняет его: человек, выбравший цели
    модулю, выбрал именно их. Сверх потолка — отказ, а не урезание: какие цели отрезать,
    решать не нам.
    """
    goals = _ids((settings or {}).get("lead_goal_ids")) or _ids(project_goals)
    if not goals:
        return [], "no_lead_goals"
    if len(goals) > MAX_LEAD_GOALS:
        return [], "too_many_goals"
    return goals, None


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _cid(value: Any) -> Optional[int]:
    text = str(value if value is not None else "").strip()
    return int(text) if text.isdigit() and int(text) > 0 else None


def _goal_sum(conv: Any, goals: Iterable[int]) -> int:
    if not isinstance(conv, dict):
        return 0
    # Ключ цели бывает и строкой: словарь мог пройти через JSON.
    return sum(int(_num(conv.get(g, conv.get(str(g))))) for g in goals)


def _pct(part: float, base: float) -> Optional[float]:
    # Сначала умножение: 7 / 100 * 100 даёт 7.000000000000001, а в журнал идёт число.
    return part * 100.0 / base if base > 0 else None


def _div(a: float, b: float) -> Optional[float]:
    return a / b if b > 0 else None


def build_rows(raw: List[Dict[str, Any]], *, goals: List[int], by: str, currency: str,
               normalize: Callable[[Any], str], key: str = "placement"
               ) -> Tuple[List[Row], Dict[int, str]]:
    """Строки отчёта одного логина за окно -> (строки правил, кампании с невидимой целью).

    raw — строки {campaign_id, <key>, clicks, cost, impressions, conversions:{goal: n}};
    key — "placement" у площадок, "query" у запросов (queries.rule_raw). Объект
    сводится через normalize: «Site.ru/» и «site.ru» — одна площадка.

    by="campaign" — строка на (кампания, объект); by="account" — на объект по логину
    целиком, campaign_id = ACCOUNT_CAMPAIGN_ID.

    Цель не видна кампании, если за окно у неё расход > 0 и ноль лидов по всем строкам:
    цель не привязана к её счётчику, и «0 лидов» здесь не факт. Такие кампании —
    в карте {campaign_id: "goal_not_visible"}; их строки (и строки по логину, куда
    они вошли) получают leads_known=False, а в базу кабинета (account.cr/cpl) не
    входят: иначе они занизили бы CR кабинета, и обычная площадка вышла бы «в разы
    лучше кабинета» (cr_x).

    Без целей лиды не известны нигде, карта пуста (причина — no_lead_goals у lead_goals).
    Неизвестные лиды и их производные — None, а не 0: пусто ≠ ноль. category строки —
    None: вердикт словаря ставит прогон, словарь здесь неизвестен.
    """
    if by not in BY:
        raise ValueError("by «%s» не из %s" % (by, BY))
    goal_list = _ids(goals)
    money_known = (currency or "RUB").upper() == "RUB"

    items = []
    for r in raw:
        cid, obj = _cid(r.get("campaign_id")), normalize(r.get(key))
        if cid is None or not obj:
            continue
        items.append((cid, obj, int(_num(r.get("clicks"))), _num(r.get("cost")),
                      int(_num(r.get("impressions"))), _goal_sum(r.get("conversions"), goal_list)))

    invisible: Dict[int, str] = {}
    if goal_list:
        cost_by: Dict[int, float] = {}
        leads_by: Dict[int, int] = {}
        for cid, _o, _cl, cost, _im, leads in items:
            cost_by[cid] = cost_by.get(cid, 0.0) + cost
            leads_by[cid] = leads_by.get(cid, 0) + leads
        invisible = {cid: "goal_not_visible" for cid in sorted(cost_by)
                     if cost_by[cid] > 0 and leads_by[cid] == 0}

    account: Dict[str, Optional[float]] = {"cr": None, "cpl": None}
    if goal_list:
        seen = [it for it in items if it[0] not in invisible]
        clicks = sum(it[2] for it in seen)
        cost = sum(it[3] for it in seen)
        leads = sum(it[5] for it in seen)
        account = {"cr": _pct(leads, clicks), "cpl": _div(cost, leads)}

    groups: Dict[Tuple[int, str], Dict[str, Any]] = {}
    for cid, obj, clicks, cost, impressions, leads in items:
        gkey = (cid if by == "campaign" else ACCOUNT_CAMPAIGN_ID, obj)
        g = groups.setdefault(gkey, {"clicks": 0, "cost": 0.0, "impressions": 0,
                                     "leads": 0, "cids": set()})
        g["clicks"] += clicks
        g["cost"] += cost
        g["impressions"] += impressions
        g["leads"] += leads
        g["cids"].add(cid)

    rows: List[Row] = []
    for (cid, obj), g in groups.items():
        known = bool(goal_list) and not (g["cids"] & invisible.keys())
        leads = g["leads"] if known else None
        rows.append(Row(
            object=obj, campaign_id=cid, category=None,
            metrics={"clicks": g["clicks"], "cost": g["cost"], "impressions": g["impressions"],
                     "ctr": _pct(g["clicks"], g["impressions"]), "leads": leads,
                     "cr": _pct(leads, g["clicks"]) if known else None,
                     "cpl": _div(g["cost"], leads) if known else None},
            account=dict(account), leads_known=known, money_known=money_known, scope=by))
    # Порядок детерминирован и полезен лимиту записи: дорогие первыми.
    rows.sort(key=lambda r: (-r.metrics["cost"], r.campaign_id, r.object))
    return rows, invisible
