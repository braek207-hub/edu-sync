# -*- coding: utf-8 -*-
"""Модуль queries: минус-фразы кампании по отчёту поисковых запросов.

Кандидатов считает конвейер агента (sync/agent_e0.py, блок минусации) на тех же
функциях sync.agent.objects; правила не меняются. Здесь — данные под эти
функции (свежий отчёт и свежий снимок ключей логина), находки и запись по
правилу движка: свежее чтение -> слияние -> запись -> чтение обратно.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

from sync.agent import objects
from sync.agent.writer import negatives
from sync.agent.writer.goal import PLACEHOLDER_GOAL_ID, read_goal_ids
from sync.modules import direct_api
from sync.modules.accounts import DirectAccount
from sync.modules.types import ApplyResult, Context, Finding, RunOutput, StoredFinding
from sync.placements.direct import DirectError

KEY = "queries"
FIELD = "NegativeKeywords"
# Минус-фразы уровня кампании есть у текстовых и ЕПК; у медийных и смартов модуль их не ставит.
TYPES = frozenset({"TEXT_CAMPAIGN", "UNIFIED_CAMPAIGN"})
STATES = frozenset({"ON"})
TYPE_BLOCKS = {"TEXT_CAMPAIGN": "TextCampaign", "UNIFIED_CAMPAIGN": "UnifiedCampaign"}
# Потолок Reports API на число целей в одном отчёте (как в placements_with_goals).
MAX_REPORT_GOALS = 10
# Горсть на кампанию за прогон — та же, что у агента за такт: связь «отсекли -> упал поток»
# должна оставаться видна по дням.
PER_CAMPAIGN = negatives.MAX_PHRASES_PER_TICK
SAMPLE = 5
# Москва без перехода на летнее время с 2014 (как в runner.MSK; импорт оттуда дал бы цикл
# runner -> registry -> queries).
MSK = timezone(timedelta(hours=3))
REPORT_FIELDS = ["CampaignId", "Query", "Criterion", "CriterionType", "Clicks", "Cost",
                 "Conversions"]
# Колонка конверсий приходит на цель: Conversions_<цель>_<модель атрибуции>.
_GOAL_COLUMN = re.compile(r"^Conversions_(\d+)_")
NO_DATA = "--"


def window(now: datetime, days: int) -> Tuple[str, str]:
    """Окно отчёта по вчера по Москве включительно: отчёт по сегодня зависает."""
    to = now.astimezone(MSK).date() - timedelta(days=1)
    return (to - timedelta(days=max(int(days), 1) - 1)).isoformat(), to.isoformat()


def _words(text: Any) -> FrozenSet[str]:
    # Тот же разбор, что у агента: иначе «своя семантика» здесь и там разошлась бы.
    return objects._phrase_words(text)


def _num(value: Any) -> float:
    text = str(value or "").strip()
    if not text or text == NO_DATA:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _goal_items(value: Any) -> List[Any]:
    # PriorityGoals Директ отдаёт то списком, то обёрткой {"Items": [...]}
    # (sync/edu_direct_settings._as_list).
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("Items"), list):
        return value["Items"]
    return []


def strategy_goals(campaign: dict) -> List[int]:
    """Цели, на которые учится кампания: цель стратегии и приоритетные цели кампании.

    GoalId 13 в стратегии — не цель, а «цели заданы приоритетными целями кампании»
    (writer/goal.PLACEHOLDER_GOAL_ID), поэтому PriorityGoals самой кампании читаем тоже.
    """
    block = campaign.get(TYPE_BLOCKS.get(campaign.get("Type"), "")) or {}
    out = set(read_goal_ids(block.get("BiddingStrategy") or {}))
    for item in _goal_items(block.get("PriorityGoals")):
        raw = item.get("GoalId") if isinstance(item, dict) else item
        try:
            goal = int(raw)
        except (TypeError, ValueError):
            continue
        if goal != PLACEHOLDER_GOAL_ID:
            out.add(goal)
    return sorted(out)


def _campaigns(acc: DirectAccount) -> List[dict]:
    # Стратегии одним вызовом с типом и состоянием: placements.direct.campaign_goals
    # читает только TextCampaign, и ЕПК без goal_ids уходили бы в пропуск молча.
    blocks = ["BiddingStrategy", "PriorityGoals"]
    return direct_api.get_all(acc.token, acc.login, "campaigns", {},
                              ["Id", "Name", "Type", "State", FIELD],
                              extra={"TextCampaignFieldNames": blocks,
                                     "UnifiedCampaignFieldNames": blocks})


def _report(acc: DirectAccount, win: Tuple[str, str], goals: List[int],
            campaign_ids: List[str]) -> List[Dict[str, str]]:
    params = {
        "SelectionCriteria": {
            "DateFrom": win[0], "DateTo": win[1],
            "Filter": [{"Field": "Clicks", "Operator": "GREATER_THAN", "Values": ["0"]},
                       {"Field": "CampaignId", "Operator": "IN",
                        "Values": sorted(campaign_ids, key=int)}],
        },
        "Goals": [str(g) for g in goals],
        # Та же модель, что у спам-правила площадок: модули одного проекта судят по одной.
        "AttributionModels": ["AUTO"],
        "FieldNames": REPORT_FIELDS,
        "ReportType": "SEARCH_QUERY_PERFORMANCE_REPORT",
        "DateRangeType": "CUSTOM_DATE",
        "Format": "TSV",
        # Расход с НДС, как во всём проекте: пороги и CPA кабинета в тех же рублях.
        "IncludeVAT": "YES",
        "IncludeDiscount": "NO",
    }
    return direct_api.report_rows(acc.token, acc.login, params, prefix="module-queries")


def _query_rows(raw: List[Dict[str, str]], goals_by_cid: Dict[str, List[int]]
                ) -> List[Dict[str, Any]]:
    """Строки отчёта -> форма функций агента, одна строка на (кампания, запрос).

    Конверсии строки — максимум по колонкам целей ЕЁ кампании, а не сумма: цели
    пересекаются (агент видел одно действие под двумя id, segments.primary_goal_column),
    и сумма удвоила бы конверсии. Ноль/не ноль от выбора не зависит.
    """
    cols: Dict[int, List[str]] = {}
    for rec in raw[:1]:
        for name in rec:
            m = _GOAL_COLUMN.match(name)
            if m:
                cols.setdefault(int(m.group(1)), []).append(name)
    agg: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for rec in raw:
        cid = str(rec.get("CampaignId") or "").strip()
        query = str(rec.get("Query") or "").strip()
        if cid not in goals_by_cid or not query:
            continue
        conv = max((_num(rec.get(c)) for g in goals_by_cid[cid] for c in cols.get(g, ())),
                   default=0.0)
        # Ключ — только у строки ключевой фразы; автотаргетинг и аудитории закупкой не являются.
        key = (rec.get("Criterion") or "") if rec.get("CriterionType") == "KEYWORD" else ""
        slot = agg.setdefault((cid, query), {"query": query, "campaign_id": cid,
                                             "cost": 0.0, "clicks": 0, "conversions": 0,
                                             "matched_key": key})
        slot["cost"] += _num(rec.get("Cost"))
        slot["clicks"] += int(_num(rec.get("Clicks")))
        slot["conversions"] += int(conv)
        if key and not slot["matched_key"]:
            slot["matched_key"] = key
    return list(agg.values())


def _key_conflict(words: FrozenSet[str], keys: Iterable[FrozenSet[str]]) -> bool:
    # Минус-фраза гасит любой запрос, где есть все её слова: ключ, содержащий их все,
    # перестанет показываться. Словоформы не сводятся (морфологии в проекте нет) —
    # проверка нижняя, как и семейства агента.
    return bool(words) and any(words <= k for k in keys)


def _never_hit(words: FrozenSet[str], never: List[FrozenSet[str]]) -> Optional[FrozenSet[str]]:
    # never_minus — «не трогать вовсе»: фраза, задевающая хоть одно его слово, не ставится.
    for item in never:
        if words & item:
            return item
    return None


def _never_list(settings: dict) -> List[FrozenSet[str]]:
    return [w for w in (_words(negatives.normalize_phrase(x))
                        for x in settings.get("never_minus") or []) if w]


def _family(rows: List[Dict[str, Any]], words: FrozenSet[str]) -> Dict[str, Any]:
    hit = sorted((q for q in rows if words <= _words(q["query"])),
                 key=lambda q: -q["cost"])
    return {"clicks": sum(q["clicks"] for q in hit),
            "cost": round(sum(q["cost"] for q in hit), 2),
            "conversions": sum(q["conversions"] for q in hit),
            "queries_sample": [q["query"] for q in hit[:SAMPLE]]}


@dataclass
class _Scan:
    findings: List[Finding] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    refused: List[str] = field(default_factory=list)
    over_cap: int = 0
    # False — кабинет целиком не судили: его открытые находки не закрываем
    judged: bool = True


def _skip_account(login: str, reason: str) -> _Scan:
    return _Scan(skipped=["%s: %s" % (login, reason)], judged=False)


def _candidates(rows, settings, cpa_limit, own_words, own_phrases):
    """(правило, фраза, кампания) в порядке агента: фразы, слова, словарь проекта."""
    mult = float(settings["multiplier"])
    clicks = sum(q["clicks"] for q in rows)
    base = sum(q["conversions"] for q in rows) / clicks if clicks else 0.0
    phrase = objects.minus_word_candidates(objects.top_queries_by_cost(rows),
                                           cpa_limit=cpa_limit, multiplier=mult,
                                           base_conversion=base)
    out: List[Tuple[str, str, str]] = []
    if phrase:
        kept, _dropped = objects.phrases_cutting_only_waste(
            phrase, rows, cpa_limit=cpa_limit, multiplier=mult, bought=own_phrases)
        out += [("phrase_waste", c["query"], cid) for c in kept for cid in c["campaigns"]]
    words = objects.word_minus_candidates(rows, cpa_limit=cpa_limit, multiplier=mult,
                                          base_conversion=base, protected_words=own_words)
    out += [("word_zero_conv", c["query"], cid) for c in words for cid in c["campaigns"]]
    cids = sorted({q["campaign_id"] for q in rows})
    for stop in settings.get("stop_words") or []:
        sw = _words(stop)
        for cid in cids:
            if sw and any(sw <= _words(q["query"]) for q in rows if q["campaign_id"] == cid):
                out.append(("stop_word", stop, cid))
    return out


def _scan_login(acc: DirectAccount, settings: dict, win: Tuple[str, str], log) -> _Scan:
    if (acc.currency or "RUB").upper() != "RUB":
        # Пороги и CPA модуля в рублях; тенге без конвертации дали бы чужой масштаб.
        return _skip_account(acc.login, "кабинет в %s, пороги модуля в рублях" % acc.currency)
    live = {str(c["Id"]): c for c in _campaigns(acc)
            if c.get("Type") in TYPES and c.get("State") in STATES}
    if not live:
        log("[%s] нечего чистить: ни одной запущенной текстовой кампании или ЕПК" % acc.login)
        return _Scan()

    scan = _Scan()

    def skip(cid, reason):
        scan.skipped.append("%s %s (%s): %s" % (acc.login, cid, live[cid].get("Name") or "",
                                                reason))

    override = sorted({int(g) for g in settings.get("goal_ids") or []})
    goals_by_cid: Dict[str, List[int]] = {}
    for cid, c in live.items():
        goals = override or strategy_goals(c)
        if goals:
            goals_by_cid[cid] = goals
        else:
            skip(cid, "судить без конверсий нельзя: у стратегии нет цели, goal_ids пуст")
    report_goals = sorted({g for gs in goals_by_cid.values() for g in gs})[:MAX_REPORT_GOALS]
    for cid in list(goals_by_cid):
        # Цель, не попавшая в отчёт, дала бы кампании «ноль конверсий» там, где их не спросили.
        if not set(goals_by_cid[cid]) <= set(report_goals):
            skip(cid, "целей в кабинете больше %d, её цели не вошли в отчёт" % MAX_REPORT_GOALS)
            del goals_by_cid[cid]
    if not goals_by_cid:
        return scan

    raw = _report(acc, win, report_goals, list(goals_by_cid))
    if raw and not any(_GOAL_COLUMN.match(k) for k in raw[0]):
        return _skip_account(acc.login, "отчёт пришёл без столбцов конверсий — судить нечем")
    rows = _query_rows(raw, goals_by_cid)

    by_cid: Dict[str, List[Dict[str, Any]]] = {}
    for q in rows:
        by_cid.setdefault(q["campaign_id"], []).append(q)
    for cid in sorted(by_cid, key=int):
        qs = by_cid[cid]
        if sum(q["cost"] for q in qs) > 0 and sum(q["conversions"] for q in qs) == 0:
            # Ноль по цели во ВСЕЙ кампании — цель не привязана к её счётчику, а не мусор.
            skip(cid, "цель не дала конверсий за окно — ноль конверсий здесь ничего не значит")
            del by_cid[cid]
    rows = [q for qs in by_cid.values() for q in qs]
    if not rows:
        return scan

    cpa_limit = settings.get("cpa_limit")
    if not cpa_limit or cpa_limit <= 0:
        conv = sum(q["conversions"] for q in rows)
        if not conv:
            return _skip_account(acc.login, "конверсий в окне нет — CPA кабинета не посчитать")
        cpa_limit = sum(q["cost"] for q in rows) / conv

    # Снимок ключей каждый прогон: matched_key отчёта — в основном подбор автотаргетинга
    # (замер агента 29.08: 1,9 % совпадений оказались ключами кабинета).
    keywords = direct_api.keywords_by_campaign(acc.token, acc.login,
                                               [int(c) for c in sorted(by_cid, key=int)])
    for cid in by_cid:
        # Кампания без ключей (только автотаргетинг) защищать законно нечего; без этого
        # own_semantics откатилась бы на matched_key отчёта.
        keywords.setdefault(cid, [])
    own_words, own_phrases = objects.own_semantics(rows, keywords)
    never = _never_list(settings)
    # Запросы до уже стоящей минус-фразы ещё лежат в окне: покрытое ею второй раз не ставим.
    covered = {cid: [w for w in (_words(p) for p in (live[cid].get(FIELD) or {}).get("Items")
                                 or []) if w]
               for cid in by_cid}

    picked: Dict[Tuple[str, str], Tuple[str, Dict[str, Any]]] = {}
    for rule, text, cid in _candidates(rows, settings, float(cpa_limit), own_words,
                                       own_phrases):
        phrase = negatives.normalize_phrase(text)
        if (cid, phrase) in picked or cid not in by_cid:
            continue
        ok, why = negatives.phrase_is_valid(phrase)
        words = _words(phrase)
        if not ok:
            scan.refused.append("%s %s «%s»: %s" % (acc.login, cid, phrase, why))
            continue
        if _never_hit(words, never) is not None:
            scan.refused.append("%s %s «%s»: в списке never_minus" % (acc.login, cid, phrase))
            continue
        if any(w <= words for w in covered[cid]):
            continue
        if _key_conflict(words, own_phrases.get(cid, ())):
            # Отказ при применении всё равно случился бы: находку, обречённую на failed, не ставим.
            scan.refused.append("%s %s «%s»: конфликт с ключом" % (acc.login, cid, phrase))
            continue
        fam = _family(by_cid[cid], words)
        if fam["clicks"] <= 0:
            continue
        picked[(cid, phrase)] = (rule, fam)

    order = sorted(picked.items(), key=lambda kv: (-kv[1][1]["cost"], kv[0]))
    per_cid: Dict[str, int] = {}
    cap = int(settings["max_per_run"])
    for (cid, phrase), (rule, fam) in order:
        if len(scan.findings) >= cap or per_cid.get(cid, 0) >= PER_CAMPAIGN:
            scan.over_cap += 1
            continue
        per_cid[cid] = per_cid.get(cid, 0) + 1
        what = "Минус-слово" if len(_words(phrase)) == 1 else "Минус-фраза"
        scan.findings.append(Finding(
            rule=rule, severity="info",
            title="%s «%s» в кампании %s" % (what, phrase, cid),
            login=acc.login, campaign_id=int(cid),
            campaign_name=live[cid].get("Name") or "",
            object_type="phrase", object_id=phrase,
            details={"clicks": fam["clicks"], "cost": fam["cost"],
                     "conversions": fam["conversions"], "window_from": win[0],
                     "window_to": win[1], "queries_sample": fam["queries_sample"]},
            action={"op": "add_negative", "campaign_id": int(cid), "phrase": phrase}))
    return scan


def run(ctx: Context) -> RunOutput:
    win = window(ctx.now, ctx.settings["window_days"])
    findings: List[Finding] = []
    skipped: List[str] = []
    refused: List[str] = []
    scanned: List[str] = []
    errors: List[str] = []
    over_cap = 0
    read = 0
    for acc in ctx.accounts:
        try:
            scan = _scan_login(acc, ctx.settings, win, ctx.log)
        except Exception as err:  # кабинет не должен ронять остальные
            errors.append("%s: %s" % (acc.login, err))
            ctx.log("[%s] ошибка: %s" % (acc.login, err))
            continue
        read += 1
        skipped += scan.skipped
        refused += scan.refused
        over_cap += scan.over_cap
        if scan.judged:
            findings += scan.findings
            scanned.append(acc.login)
    if errors and not read:
        raise RuntimeError("ни один логин не прочитан: " + "; ".join(errors))
    summary: Dict[str, Any] = {"window": list(win), "skipped": skipped, "refused": refused,
                               "over_cap": over_cap}
    if errors:
        summary["errors"] = errors
    # Упавший и пропущенный целиком логин не судили: его открытые находки не закрываем.
    return RunOutput(findings=findings, summary=summary, scanned_logins=scanned)


def _fresh(acc: DirectAccount, campaign_id: int):
    """Тип, состояние и минус-фразы кампании свежим чтением; None — API её не видит."""
    rows = direct_api.get_all(acc.token, acc.login, "campaigns", {"Ids": [int(campaign_id)]},
                              ["Id", "Type", "State", FIELD])
    if not rows:
        return None
    row = rows[0]
    return row.get("Type"), row.get("State"), list((row.get(FIELD) or {}).get("Items") or [])


def _write(acc: DirectAccount, campaign_id: int, items: Optional[List[str]]) -> None:
    res = direct_api.update_campaign_list(acc.token, acc.login, campaign_id, FIELD, items)
    errors = (res or {}).get("Errors") or []
    if errors:
        raise DirectError("; ".join(
            "%s %s" % (e.get("Code"), e.get("Message")) for e in errors))


def _chars(items: Iterable[str]) -> int:
    # Счёт как у рельсы агента (guardrails._check_negatives): длина фразы целиком.
    return sum(len(str(p or "").strip()) for p in items)


def _groups(items: List[StoredFinding]) -> Dict[Tuple[str, int], List[StoredFinding]]:
    out: Dict[Tuple[str, int], List[StoredFinding]] = {}
    for f in items:
        out.setdefault((f.login, int(f.action["campaign_id"])), []).append(f)
    return out


def _fail_all(group, error) -> Dict[int, ApplyResult]:
    return {f.id: ApplyResult(False, {}, error) for f in group}


def _phrase(f: StoredFinding) -> str:
    return negatives.normalize_phrase(f.action["phrase"])


def _apply_group(acc, cid, group, settings) -> Dict[int, ApplyResult]:
    fresh = _fresh(acc, cid)
    if fresh is None:
        return _fail_all(group, "кампания не видна API (Мастер кампаний)")
    ctype, state, existing = fresh
    if ctype not in TYPES:
        return _fail_all(group, "минус-фразы кампании типа %s модуль не ставит" % ctype)
    if state not in STATES:
        return _fail_all(group, "кампания не запущена (состояние %s)" % state)
    # Ключи — свежим чтением: между прогоном и одобрением ключ могли добавить руками.
    keys = [_words(k) for k in direct_api.keywords_by_campaign(
        acc.token, acc.login, [int(cid)]).get(str(cid), [])]
    never = _never_list(settings)
    have = {negatives.normalize_phrase(p) for p in existing}
    used = _chars(existing)
    out: Dict[int, ApplyResult] = {}
    to_add: List[str] = []
    for f in group:
        phrase = _phrase(f)
        words = _words(phrase)
        ok, why = negatives.phrase_is_valid(phrase)
        hit = _never_hit(words, never)
        if not ok:
            out[f.id] = ApplyResult(False, {}, why)
        elif hit is not None:
            out[f.id] = ApplyResult(False, {}, "фраза задевает never_minus «%s»"
                                    % " ".join(sorted(hit)))
        elif _key_conflict(words, keys):
            out[f.id] = ApplyResult(False, {}, "конфликт с ключом кампании")
        elif phrase in have:
            # Не наша запись: «применено» отдало бы ручную фразу под откат модуля.
            out[f.id] = ApplyResult(False, {}, "фраза уже стоит в кампании — поставлена не модулем")
        elif phrase in to_add:
            continue
        elif used + len(phrase) > negatives.MAX_TOTAL_CHARS:
            # Список сверх лимита Директ отклонит целиком: не поместившаяся фраза не едет.
            out[f.id] = ApplyResult(False, {}, "список минус-фраз заполнен (%d из %d символов)"
                                    % (used, negatives.MAX_TOTAL_CHARS))
        else:
            to_add.append(phrase)
            used += len(phrase)
    # Прежние фразы остаются как есть и первыми: в списке есть и ручные минус-фразы.
    if to_add:
        _write(acc, cid, existing + to_add)
    back = direct_api.read_campaign_list(acc.token, acc.login, cid, FIELD)
    present = {negatives.normalize_phrase(p) for p in (back or [])}
    for f in group:
        if f.id in out:
            continue
        phrase = _phrase(f)
        if phrase in present:
            out[f.id] = ApplyResult(True, {"campaign_id": cid, "phrase": phrase,
                                           "chars_after": _chars(back or [])})
        else:
            out[f.id] = ApplyResult(False, {"campaign_id": cid, "phrase": phrase},
                                    "Директ принял, но не применил")
    return out


def _rollback_group(acc, cid, group, settings) -> Dict[int, ApplyResult]:
    fresh = _fresh(acc, cid)
    if fresh is None:
        return _fail_all(group, "кампания не видна API (Мастер кампаний)")
    existing = fresh[2]
    # Вычесть ровно своё из свежего списка (writer/negatives.remove_added), порядок чужих
    # фраз сохранить: между применением и откатом список правят руками.
    drop = {_phrase(f) for f in group}
    keep = [p for p in existing if negatives.normalize_phrase(p) not in drop]
    if len(keep) != len(existing):
        # Пустой список — null: NegativeKeywords в campaigns.update nillable, а Items в нём
        # обязателен (direct_api.update_campaign_list).
        _write(acc, cid, keep or None)
    back = direct_api.read_campaign_list(acc.token, acc.login, cid, FIELD)
    present = {negatives.normalize_phrase(p) for p in (back or [])}
    out: Dict[int, ApplyResult] = {}
    for f in group:
        phrase = _phrase(f)
        if back is not None and phrase not in present:
            out[f.id] = ApplyResult(True, {"campaign_id": cid, "phrase": phrase})
        else:
            out[f.id] = ApplyResult(False, {"campaign_id": cid, "phrase": phrase},
                                    "минус-фраза осталась в списке после снятия")
    return out


def _run_groups(ctx: Context, items: List[StoredFinding], handler) -> Dict[int, ApplyResult]:
    by_login = {a.login: a for a in ctx.accounts}
    out: Dict[int, ApplyResult] = {}
    for (login, cid), group in _groups(items).items():
        acc = by_login.get(login)
        if acc is None:
            out.update(_fail_all(group, "кабинет %s недоступен" % login))
            continue
        try:
            out.update(handler(acc, cid, group, ctx.settings))
        except Exception as err:  # одна кампания не должна ронять остальные
            out.update(_fail_all(group, "%s: %s" % (type(err).__name__, err)))
    return out


def apply(ctx: Context, items: List[StoredFinding]) -> Dict[int, ApplyResult]:
    return _run_groups(ctx, items, _apply_group)


def rollback(ctx: Context, items: List[StoredFinding]) -> Dict[int, ApplyResult]:
    return _run_groups(ctx, items, _rollback_group)
