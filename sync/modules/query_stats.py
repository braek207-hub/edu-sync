# -*- coding: utf-8 -*-
"""Статистика поисковых запросов по дням для страницы «Чистильщик запросов» в Panda-BI
(module_query_texts, module_query_stats — миграция Panda 20261107221317_module_query_stats.sql).

Страница считает предложения за любой выбранный период, поэтому храним не окно скана, а дни:
запрос × кампания × день, только с кликами (без кликов нет расхода — резать нечего).
Пишет скан модуля queries перед своим run (runner вызывает prepare), раз в сутки:
первый прогон кабинета — FIRST_DAYS, дальше от последнего дня, но не позже RELOAD_DAYS назад —
конверсии по атрибуции доезжают днями, свежий хвост перезаливается целиком.
Старше KEEP_DAYS — удаляется тем же прогоном вместе с текстами без строк.

Конверсии — по целям-лидам модуля (lead_goal_ids, иначе цели проекта), а если их нет — по
целям стратегии кампании, как у встроенных правил скана. Кампания без целей или отчёт без
столбцов конверсий — conversions None: «не знаем», а не ноль.
"""

from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sync.modules import direct_api, leads
from sync.modules import queries as Q
from sync.modules.accounts import DirectAccount
from sync.modules.types import Context
from sync.placements.direct import MAX_REPORT_GOALS

FIRST_DAYS = 90
RELOAD_DAYS = 10
# Замер 08.11 на 90 днях: 914 тыс. строк, 353 МБ (EDU — 91 %: запросы почти все уникальны,
# словарь текстов их не сжимает). 180 дней удвоили бы объём; период страницы — до 90 дней.
KEEP_DAYS = 90
# Директ отдаёт запрос до 4096 символов, а колонка texts ограничена 1000 (btree UNIQUE).
MAX_QUERY = 1000

Row = Tuple[str, int, str, int, int, float, Optional[float]]


def span(last: Optional[date], yesterday: date) -> Tuple[date, date]:
    """Дни загрузки кабинета: (с, по) включительно."""
    first = yesterday - timedelta(days=FIRST_DAYS - 1)
    if last is None:
        return first, yesterday
    tail = yesterday - timedelta(days=RELOAD_DAYS - 1)
    return max(first, min(last + timedelta(days=1), tail)), yesterday


def _report(acc: DirectAccount, start: date, end: date, goals: List[int],
            campaign_ids: List[str]) -> List[Dict[str, str]]:
    # Те же параметры, что у отчёта скана (queries._report): НДС, без скидки, модель AUTO.
    fields = ["Date"] + [f for f in Q.REPORT_FIELDS if goals or f != "Conversions"]
    params: Dict[str, Any] = {
        "SelectionCriteria": {
            "DateFrom": start.isoformat(), "DateTo": end.isoformat(),
            "Filter": [{"Field": "Clicks", "Operator": "GREATER_THAN", "Values": ["0"]},
                       {"Field": "CampaignId", "Operator": "IN",
                        "Values": sorted(campaign_ids, key=int)}],
        },
        "FieldNames": fields,
        "ReportType": "SEARCH_QUERY_PERFORMANCE_REPORT",
        "DateRangeType": "CUSTOM_DATE",
        "Format": "TSV",
        "IncludeVAT": "YES",
        "IncludeDiscount": "NO",
    }
    if goals:
        params["Goals"] = [str(g) for g in goals]
        params["AttributionModels"] = ["AUTO"]
    return direct_api.report_rows(acc.token, acc.login, params, prefix="module-query-stats")


def rows(raw: List[Dict[str, str]], goals_by_cid: Dict[str, List[int]]) -> List[Row]:
    """Строки отчёта -> (день, кампания, запрос, показы, клики, расход, конверсии).

    Конверсии строки — максимум по столбцам целей её кампании, как в queries._query_rows:
    цели пересекаются, сумма удвоила бы. Нет столбцов целей кампании — None."""
    cols: Dict[int, List[str]] = {}
    for rec in raw[:1]:
        for name in rec:
            m = Q._GOAL_COLUMN.match(name)
            if m:
                cols.setdefault(int(m.group(1)), []).append(name)
    agg: Dict[Tuple[str, int, str], List[Any]] = {}
    for rec in raw:
        day = str(rec.get("Date") or "").strip()
        cid = str(rec.get("CampaignId") or "").strip()
        query = str(rec.get("Query") or "").strip()[:MAX_QUERY]
        if not day or not cid.isdigit() or not query:
            continue
        names = [c for g in goals_by_cid.get(cid, ()) for c in cols.get(g, ())]
        conv = max(Q._num(rec.get(c)) for c in names) if names else None
        slot = agg.setdefault((day, int(cid), query), [0, 0, 0.0, None])
        slot[0] += int(Q._num(rec.get("Impressions")))
        slot[1] += int(Q._num(rec.get("Clicks")))
        slot[2] += Q._num(rec.get("Cost"))
        if conv is not None:
            slot[3] = (slot[3] or 0.0) + conv
    return [(d, c, q, s[0], s[1], round(s[2], 2), s[3]) for (d, c, q), s in agg.items()]


def batches(campaigns: List[dict], lead: List[int]
            ) -> List[Tuple[List[str], List[int], Dict[str, List[int]]]]:
    """Отчёты кабинета: (кампании, цели отчёта, цели по кампании).

    В отчёт входит не больше MAX_REPORT_GOALS целей, а у кабинета на сотни кампаний целей
    стратегий больше (LIME 07.11: 875 кампаний, первые 10 id по возрастанию отрезали цель
    покупки у 675 из них). Поэтому кампании раскладываются по отчётам: наборы целей от самых
    частых к редким, каждый — в первый отчёт, куда помещается. Цели-лиды — одни на все
    кампании, отчёт один. Кампания без целей или с набором больше потолка едет в первом
    отчёте без целей: её конверсии None."""
    cids = [str(c["Id"]) for c in campaigns]
    if lead:
        return [(cids, list(lead), {cid: list(lead) for cid in cids})]
    sets: Dict[Tuple[int, ...], List[str]] = {}
    blind: List[str] = []
    for c in campaigns:
        goals = tuple(Q.strategy_goals(c))
        if goals and len(goals) <= MAX_REPORT_GOALS:
            sets.setdefault(goals, []).append(str(c["Id"]))
        else:
            blind.append(str(c["Id"]))
    out: List[Tuple[List[str], set, Dict[str, List[int]]]] = []
    for goals, ids in sorted(sets.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        for b in out:
            if len(b[1] | set(goals)) <= MAX_REPORT_GOALS:
                break
        else:
            b = ([], set(), {})
            out.append(b)
        b[0].extend(ids)
        b[1].update(goals)
        b[2].update({cid: list(goals) for cid in ids})
    if blind:
        if not out:
            out.append(([], set(), {}))
        out[0][0].extend(blind)
    return [(ids, sorted(goals), by_cid) for ids, goals, by_cid in out]


def load_login(acc: DirectAccount, store, slug: str, lead: List[int], yesterday: date
               ) -> Optional[dict]:
    """Загрузить дни кабинета. None — кампаний с поиском нет, грузить нечего."""
    camps = [c for c in Q._campaigns(acc) if c.get("Type") in Q.TYPES]
    if not camps:
        return None
    start, end = span(store.query_stats_last_day(slug, acc.login), yesterday)
    got: List[Row] = []
    plan = batches(camps, lead)
    for ids, goals, by_cid in plan:
        raw = _report(acc, start, end, goals, ids)
        if goals and raw and not any(Q._GOAL_COLUMN.match(k) for k in raw[0]):
            by_cid = {}
        got += rows(raw, by_cid)
    store.replace_query_stats(slug, acc.login, start, end, got)
    return {"from": start.isoformat(), "to": end.isoformat(), "rows": len(got),
            "reports": len(plan)}


def prepare(ctx: Context, store) -> dict:
    """Шаг перед сканом queries. Сбой кабинета — строка в errors, остальные грузятся:
    статистика страницы не должна ронять скан, и скан — её."""
    settings, _notes = Q.checked_settings(ctx.settings)
    lead, _code = leads.lead_goals(settings, ctx.project_goals)
    yesterday = date.fromisoformat(Q.window(ctx.now, 1)[1])
    out: Dict[str, Any] = {"logins": {}}
    errors: List[str] = []
    for acc in ctx.accounts:
        try:
            got = load_login(acc, store, ctx.slug, lead, yesterday)
        except Exception as err:  # кабинет не должен ронять остальные
            errors.append("%s: %s: %s" % (acc.login, type(err).__name__, err))
            ctx.log("[query_stats %s] ошибка: %s" % (acc.login, err))
            continue
        if got is not None:
            out["logins"][acc.login] = got
            ctx.log("[query_stats %s] %s…%s: %d строк" % (acc.login, got["from"], got["to"],
                                                          got["rows"]))
    try:
        out["pruned"] = store.prune_query_stats(
            ctx.slug, yesterday - timedelta(days=KEEP_DAYS - 1))
    except Exception as err:
        errors.append("prune: %s: %s" % (type(err).__name__, err))
    if errors:
        out["errors"] = errors
    return out
