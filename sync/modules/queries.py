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
from sync.modules.text import yo
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
REPORT_FIELDS = ["CampaignId", "Query", "Clicks", "Cost", "Conversions"]
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
        # matched_key пуст намеренно: «что мы купили» берётся только из свежего снимка
        # ключей (каждая судимая кампания в нём есть), колонке отчёта не верим.
        slot = agg.setdefault((cid, query), {"query": query, "campaign_id": cid,
                                             "cost": 0.0, "clicks": 0, "conversions": 0,
                                             "matched_key": ""})
        slot["cost"] += _num(rec.get("Cost"))
        slot["clicks"] += int(_num(rec.get("Clicks")))
        slot["conversions"] += int(conv)
    return list(agg.values())


# Минус-слово Директ применяет ко всем словоформам, а морфологии в проекте нет. Сравнение
# по общему началу — грубое и только запрещающее: ложное совпадение лишь снимает кандидата,
# пропущенное отрезало бы свой ключ. Короткие слова совпадают только целиком.
STEM_MIN = 4
STEM_TAIL = 2


def _fold(words: FrozenSet[str]) -> FrozenSet[str]:
    return frozenset(yo(w) for w in words)


def _same_stem(a: str, b: str) -> bool:
    a, b = yo(a), yo(b)
    if a == b:
        return True
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n >= max(STEM_MIN, min(len(a), len(b)) - STEM_TAIL)


def _covers(words: FrozenSet[str], target: FrozenSet[str]) -> bool:
    """Гасит ли минус-фраза words текст со словами target: каждое её слово есть там в какой-то форме."""
    return bool(words) and all(any(_same_stem(w, t) for t in target) for w in words)


def _key_conflict(words: FrozenSet[str], keys: Iterable[FrozenSet[str]]) -> bool:
    # Минус-фраза гасит любой запрос, где есть все её слова: ключ, содержащий их все
    # (в любой форме), перестанет показываться.
    return any(_covers(words, k) for k in keys)


def _never_hit(words: FrozenSet[str], never: List[FrozenSet[str]]) -> Optional[FrozenSet[str]]:
    # never_minus — «не трогать вовсе»: фраза, задевающая хоть одно его слово в любой форме,
    # не ставится.
    for item in never:
        if any(_same_stem(w, n) for w in words for n in item):
            return item
    return None


def _never_list(settings: dict) -> List[FrozenSet[str]]:
    return [w for w in (_words(negatives.normalize_phrase(x))
                        for x in settings.get("never_minus") or []) if w]


def _family(rows: List[Dict[str, Any]], words: FrozenSet[str], stems: bool) -> Dict[str, Any]:
    """Поток кампании под минус-фразой. stems=False — точные формы (ё=е): это видно в
    находке. stems=True — с грубыми словоформами: только для отказов, где лишнее совпадение
    снимает кандидата, а не добавляет минус."""
    if stems:
        hit = [q for q in rows if _covers(words, _words(q["query"]))]
    else:
        hit = [q for q in rows if _fold(words) <= _fold(_words(q["query"]))]
    hit.sort(key=lambda q: -q["cost"])
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

    def skip_account(self, login: str, reason: str) -> "_Scan":
        # Причины по кампаниям, собранные до пропуска кабинета, остаются в отчёте.
        self.skipped.append("%s: %s" % (login, reason))
        self.findings = []
        self.judged = False
        return self


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
            # Стоп-слово ищется точной формой (с ё=е), а не по основе: оно добавляет минус, и
            # ложная основа («скачать»/«скачки») дала бы лишний. Основа — только для отказов.
            if sw and any(_fold(sw) <= _fold(_words(q["query"]))
                          for q in rows if q["campaign_id"] == cid):
                out.append(("stop_word", stop, cid))
    return out


def _scan_login(acc: DirectAccount, settings: dict, win: Tuple[str, str], log) -> _Scan:
    scan = _Scan()
    if (acc.currency or "RUB").upper() != "RUB":
        # Пороги и CPA модуля в рублях; тенге без конвертации дали бы чужой масштаб.
        return scan.skip_account(acc.login, "кабинет в %s, пороги модуля в рублях"
                                 % acc.currency)
    live = {str(c["Id"]): c for c in _campaigns(acc)
            if c.get("Type") in TYPES and c.get("State") in STATES}
    if not live:
        log("[%s] нечего чистить: ни одной запущенной текстовой кампании или ЕПК" % acc.login)
        return scan

    def skip(cid, reason):
        scan.skipped.append("%s %s (%s): %s" % (acc.login, cid, live[cid].get("Name") or "",
                                                reason))

    def refuse(cid, phrase, reason):
        scan.refused.append("%s %s «%s»: %s" % (acc.login, cid, phrase, reason))

    override = settings["lead_goal_ids"]
    goals_by_cid: Dict[str, List[int]] = {}
    for cid, c in live.items():
        goals = override or strategy_goals(c)
        if goals:
            goals_by_cid[cid] = goals
        else:
            skip(cid, "судить без конверсий нельзя: у стратегии нет цели, lead_goal_ids пуст")
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
        return scan.skip_account(acc.login, "отчёт пришёл без столбцов конверсий — судить нечем")
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
            return scan.skip_account(acc.login, "конверсий в окне нет — CPA кабинета не посчитать")
        cpa_limit = sum(q["cost"] for q in rows) / conv
    payoff = float(cpa_limit) * float(settings["multiplier"])

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
    seen = set()  # (кампания, фраза с ё=е): «учёба» и «учеба» — одна запись в Директе
    for rule, text, cid in _candidates(rows, settings, float(cpa_limit), own_words,
                                       own_phrases):
        phrase = negatives.normalize_phrase(text)
        if (cid, yo(phrase)) in seen or cid not in by_cid:
            continue
        ok, why = negatives.phrase_is_valid(phrase)
        words = _words(phrase)
        if not ok:
            refuse(cid, phrase, why)
            continue
        if _never_hit(words, never) is not None:
            refuse(cid, phrase, "в списке never_minus")
            continue
        if any(_covers(w, words) for w in covered[cid]):
            continue
        if _key_conflict(words, own_phrases.get(cid, ())):
            # Отказ при применении всё равно случился бы: находку, обречённую на failed, не ставим.
            refuse(cid, phrase, "конфликт с ключом")
            continue
        fam = _family(by_cid[cid], words, stems=False)
        if fam["clicks"] <= 0:
            continue
        wide = _family(by_cid[cid], words, stems=True)
        if rule == "word_zero_conv" and wide["conversions"] > 0:
            # Агент судил слово в точной форме; Директ отрежет и формы, а у них конверсии есть.
            refuse(cid, phrase, "словоформы слова приносят конверсии")
            continue
        paying = [f for f in (fam, wide)
                  if f["conversions"] > 0 and f["cost"] / f["conversions"] <= payoff]
        if rule == "phrase_waste" and paying:
            # Семейство агент проверял по всему кабинету; запрет пишется в кампанию, и там,
            # где поток окупается (в точных формах или со словоформами), ему не место.
            refuse(cid, phrase, "в этой кампании семейство окупается (CPA %.0f ₽)"
                   % (paying[0]["cost"] / paying[0]["conversions"]))
            continue
        picked[(cid, phrase)] = (rule, fam)
        seen.add((cid, yo(phrase)))

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


# Границы — те же, что у формы настроек в Panda-BI (lib/modules/catalog.ts): значение вне
# них туда не ввести, значит пришло мимо формы, и молча подставлять умолчание нельзя.
BOUNDS = {"window_days": (7, 90), "multiplier": (1, 10), "max_per_run": (1, 100)}


def checked_settings(settings: dict) -> Tuple[dict, List[str]]:
    """Настройки после settings_for -> (годные, заметки об отброшенном). Вне границ — ошибка."""
    bad = ["%s=%r вне [%s, %s]" % (k, settings[k], lo, hi)
           for k, (lo, hi) in BOUNDS.items() if not lo <= settings[k] <= hi]
    if settings["cpa_limit"] is not None and settings["cpa_limit"] < 0:
        bad.append("cpa_limit=%r меньше 0" % settings["cpa_limit"])
    if bad:
        raise ValueError("неверные настройки: " + "; ".join(bad))
    out = dict(settings)
    notes: List[str] = []
    goals = set()
    for g in settings["lead_goal_ids"]:
        text = str(g).strip()
        if isinstance(g, bool) or not text.isdigit():
            notes.append("lead_goal_ids: «%s» не идентификатор цели — отброшено" % g)
        else:
            goals.add(int(text))
    out["lead_goal_ids"] = sorted(goals)
    for key in ("stop_words", "never_minus"):
        good = [x for x in settings[key] if isinstance(x, str) and x.strip()]
        if len(good) != len(settings[key]):
            notes.append("%s: нестроковые и пустые элементы отброшены" % key)
        out[key] = good
    return out, notes


def run(ctx: Context) -> RunOutput:
    settings, notes = checked_settings(ctx.settings)
    win = window(ctx.now, settings["window_days"])
    findings: List[Finding] = []
    skipped: List[str] = []
    refused: List[str] = []
    scanned: List[str] = []
    errors: List[str] = []
    over_cap = 0
    read = 0
    for acc in ctx.accounts:
        try:
            scan = _scan_login(acc, settings, win, ctx.log)
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
    if notes:
        summary["settings_notes"] = notes
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


NOT_FOUND_BACK = "кампания не найдена при чтении обратно"


def _write_and_read(acc: DirectAccount, cid: int, items: Optional[List[str]]):
    """Запись и чтение обратно: (что в списке сейчас, ошибка записи или None).

    Таймаут мог прийти уже после того, как Директ запись принял, поэтому чтение идёт
    при любом исходе записи, и решает оно (как placements._write_and_read). Не
    прочиталось и оно: исход неизвестен, ошибка уходит наверх."""
    werr = None
    try:
        _write(acc, cid, items)
    except Exception as err:
        werr = err
    try:
        back = direct_api.read_campaign_list(acc.token, acc.login, cid, FIELD)
    except Exception as rerr:
        raise werr or rerr
    return back, werr


def _write_error(err: Optional[Exception]) -> Optional[str]:
    return "%s: %s" % (type(err).__name__, err) if err is not None else None


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
    have = {yo(negatives.normalize_phrase(p)) for p in existing}
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
        elif yo(phrase) in have:
            # Не наша запись: «применено» отдало бы ручную фразу под откат модуля.
            out[f.id] = ApplyResult(False, {}, "фраза уже стоит в кампании — поставлена не модулем")
        elif yo(phrase) in {yo(x) for x in to_add}:
            # Вторая находка на то же действие: запись одна, засчитать её дважды нельзя.
            out[f.id] = ApplyResult(False, {}, "дубль: та же фраза в этом применении")
        elif used + len(phrase) > negatives.MAX_TOTAL_CHARS:
            # Список сверх лимита Директ отклонит целиком: не поместившаяся фраза не едет.
            out[f.id] = ApplyResult(False, {}, "список минус-фраз заполнен (%d из %d символов)"
                                    % (used, negatives.MAX_TOTAL_CHARS))
        else:
            to_add.append(phrase)
            used += len(phrase)
    if not to_add:
        return out
    # Прежние фразы остаются как есть и первыми: в списке есть и ручные минус-фразы.
    back, werr = _write_and_read(acc, cid, existing + to_add)
    if back is None:
        out.update({f.id: ApplyResult(False, {}, NOT_FOUND_BACK) for f in group
                    if f.id not in out})
        return out
    present = {negatives.normalize_phrase(p) for p in back}
    for f in group:
        if f.id in out:
            continue
        phrase = _phrase(f)
        if phrase in present:
            out[f.id] = ApplyResult(True, {"campaign_id": cid, "phrase": phrase,
                                           "chars_after": _chars(back)})
        else:
            out[f.id] = ApplyResult(False, {"campaign_id": cid, "phrase": phrase},
                                    _write_error(werr) or "Директ принял, но не применил")
    return out


def _rollback_group(acc, cid, group, settings) -> Dict[int, ApplyResult]:
    fresh = _fresh(acc, cid)
    if fresh is None:
        return _fail_all(group, "кампания не видна API (Мастер кампаний)")
    existing = fresh[2]
    out: Dict[int, ApplyResult] = {}
    drop: List[str] = []
    dups = set()
    for f in group:
        # Модуль пишет нормализованную форму: своё — только точное совпадение. Вариант в
        # другом регистре или с другими пробелами поставил человек (writer/negatives.remove_added
        # вычитает «ровно своё», а не похожее).
        phrase = _phrase(f)
        if phrase in drop:
            # Вторая находка на ту же фразу: судит то же чтение обратно, что и первую. failed
            # вернул бы её в applied, хотя фразы уже нет.
            dups.add(f.id)
        elif any(p.strip() == phrase for p in existing):
            drop.append(phrase)
        elif any(yo(negatives.normalize_phrase(p)) == yo(phrase) for p in existing):
            out[f.id] = ApplyResult(False, {"campaign_id": cid, "phrase": phrase},
                                    "вариант поставлен не модулем")
        else:
            out[f.id] = ApplyResult(True, {"campaign_id": cid, "phrase": phrase,
                                           "already_absent": True})
    if not drop:
        return out
    keep = [p for p in existing if p.strip() not in drop]
    # Пустой список — null: NegativeKeywords в campaigns.update nillable, а Items в нём
    # обязателен (direct_api.update_campaign_list).
    back, werr = _write_and_read(acc, cid, keep or None)
    if back is None:
        out.update({f.id: ApplyResult(False, {}, NOT_FOUND_BACK) for f in group
                    if f.id not in out})
        return out
    present = {p.strip() for p in back}
    for f in group:
        if f.id in out:
            continue
        phrase = _phrase(f)
        if phrase not in present:
            resp = {"campaign_id": cid, "phrase": phrase}
            if f.id in dups:
                resp["already_absent"] = True
            out[f.id] = ApplyResult(True, resp)
        else:
            out[f.id] = ApplyResult(False, {"campaign_id": cid, "phrase": phrase},
                                    _write_error(werr)
                                    or "минус-фраза осталась в списке после снятия")
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
