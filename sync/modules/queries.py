# -*- coding: utf-8 -*-
"""Модуль queries: минус-фразы кампании по отчёту поисковых запросов.

Порядок решений по минус-фразе в кампании:
1. защиты — пользовательский protect (по запросам, которые фраза отрезала бы),
   «Не минусовать», затем покрытость минусом и ключи кампании;
2. встроенные этапа 1: «Фраза не окупается», «Слово без конверсий» (выключаются
   настройкой builtin), стоп-слова;
3. пользовательские правила по строке запрос × кампания (leads.build_rows).

Кандидатов встроенных считает конвейер агента (sync/agent_e0.py, блок минусации) на
тех же функциях sync.agent.objects; правила не меняются, fingerprint тот же. Лимит
max_per_run и 10 на кампанию — общий, встроенные берут его первыми. Запись — по
правилу движка: свежее чтение -> слияние -> запись -> чтение обратно.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from sync.agent import objects
from sync.agent.writer import negatives
from sync.agent.writer.goal import PLACEHOLDER_GOAL_ID, read_goal_ids
from sync.modules import direct_api, leads
from sync.modules import rules as R
from sync.modules.accounts import DirectAccount
from sync.modules.reasons import by_rule, refusal
from sync.modules.text import word_list, yo
from sync.modules.types import ApplyResult, Context, Finding, RunOutput, StoredFinding
from sync.placements.direct import MAX_REPORT_GOALS, DirectError

KEY = "queries"
FIELD = "NegativeKeywords"
# Минус-фразы уровня кампании есть у текстовых и ЕПК; у медийных и смартов модуль их не ставит.
TYPES = frozenset({"TEXT_CAMPAIGN", "UNIFIED_CAMPAIGN"})
STATES = frozenset({"ON"})
TYPE_BLOCKS = {"TEXT_CAMPAIGN": "TextCampaign", "UNIFIED_CAMPAIGN": "UnifiedCampaign"}
# Горсть на кампанию за прогон — та же, что у агента за такт: связь «отсекли -> упал поток»
# должна оставаться видна по дням.
PER_CAMPAIGN = negatives.MAX_PHRASES_PER_TICK
SAMPLE = 5
# Москва без перехода на летнее время с 2014 (как в runner.MSK; импорт оттуда дал бы цикл
# runner -> registry -> queries).
MSK = timezone(timedelta(hours=3))
# Показы — для условий по CTR в пользовательских правилах: без них ctr не определён.
REPORT_FIELDS = ["CampaignId", "Query", "Impressions", "Clicks", "Cost", "Conversions"]
# Колонка конверсий приходит на цель: Conversions_<цель>_<модель атрибуции>.
_GOAL_COLUMN = re.compile(r"^Conversions_(\d+)_")
NO_DATA = "--"
# Префикс правила пользователя в fingerprint и сводке — как у площадок: id правила не
# должен совпасть с именем встроенного.
USER = "u:"
# Встроенные, которые выключает настройка builtin. Стоп-слова выключаются пустым списком.
TOGGLED = ("phrase_waste", "word_zero_conv")
BUILTIN_NAMES = {"phrase_waste": "Фраза не окупается", "word_zero_conv": "Слово без конверсий",
                 "stop_word": "Стоп-слова"}
PROPOSE = frozenset({"propose_word", "propose_phrase"})
NON_RUB = "кабинет в %s, пороги модуля в рублях"
BLIND = "отчёт пришёл без столбцов конверсий — судить нечем"
NO_CPA = "конверсий в окне нет — CPA кабинета не посчитать"


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
    params: Dict[str, Any] = {
        "SelectionCriteria": {
            "DateFrom": win[0], "DateTo": win[1],
            "Filter": [{"Field": "Clicks", "Operator": "GREATER_THAN", "Values": ["0"]},
                       {"Field": "CampaignId", "Operator": "IN",
                        "Values": sorted(campaign_ids, key=int)}],
        },
        # Без Goals столбец Conversions — сумма по всем целям счётчика: лиды и конверсии
        # посчитались бы не по тем целям, поэтому он только вместе с Goals.
        "FieldNames": [f for f in REPORT_FIELDS if goals or f != "Conversions"],
        "ReportType": "SEARCH_QUERY_PERFORMANCE_REPORT",
        "DateRangeType": "CUSTOM_DATE",
        "Format": "TSV",
        # Расход с НДС, как во всём проекте: пороги и CPA кабинета в тех же рублях.
        "IncludeVAT": "YES",
        "IncludeDiscount": "NO",
    }
    if goals:
        params["Goals"] = [str(g) for g in goals]
        # Та же модель, что у спам-правила площадок: модули одного проекта судят по одной.
        params["AttributionModels"] = ["AUTO"]
    return direct_api.report_rows(acc.token, acc.login, params, prefix="module-queries")


def _report_goals(builtin: Dict[str, List[int]], lead: List[int]
                  ) -> Tuple[List[int], List[str]]:
    """(цели отчёта, кампании встроенных, чьи цели в отчёт не вошли).

    В отчёт помещается не больше MAX_REPORT_GOALS целей. Цели-лиды идут первыми: их
    не больше потолка (leads.lead_goals сверх него отказывает с too_many_goals), так что
    пользовательские правила получают их всегда. Остаток мест — целям стратегий по
    возрастанию id, как в этапе 1. Кампания, чьи цели не поместились, встроенными не
    судится: её «ноль конверсий» был бы по неспрошенной цели (отказ report_goals_over_cap,
    тройки встроенных в unverified).
    """
    first = set(lead)
    rest = sorted({g for gs in builtin.values() for g in gs} - first)
    chosen = first | set(rest[:max(MAX_REPORT_GOALS - len(first), 0)])
    return sorted(chosen), [cid for cid, gs in builtin.items() if not set(gs) <= chosen]


def _query_rows(raw: List[Dict[str, str]], goals_by_cid: Dict[str, List[int]],
                keep: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """Строки отчёта -> форма функций агента, одна строка на (кампания, запрос).

    Конверсии строки — максимум по колонкам целей ЕЁ кампании, а не сумма: цели
    пересекаются (агент видел одно действие под двумя id, segments.primary_goal_column),
    и сумма удвоила бы конверсии. Ноль/не ноль от выбора не зависит.

    goal_conversions — достижения по КАЖДОЙ цели отчёта, а не только целям кампании:
    пользовательские правила считают лиды по целям-лидам проекта (rule_raw).

    keep — кампании без целей встроенных, которые всё равно нужны правилам
    пользователя: их conversions — 0, встроенные их не судят.
    """
    wanted = set(goals_by_cid) | set(keep)
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
        if cid not in wanted or not query:
            continue
        conv = max((_num(rec.get(c)) for g in goals_by_cid.get(cid, ())
                    for c in cols.get(g, ())), default=0.0)
        # matched_key пуст намеренно: «что мы купили» берётся только из свежего снимка
        # ключей (каждая судимая кампания в нём есть), колонке отчёта не верим.
        slot = agg.setdefault((cid, query), {"query": query, "campaign_id": cid,
                                             "cost": 0.0, "clicks": 0, "impressions": 0,
                                             "conversions": 0, "goal_conversions": {},
                                             "matched_key": ""})
        slot["cost"] += _num(rec.get("Cost"))
        slot["clicks"] += int(_num(rec.get("Clicks")))
        slot["impressions"] += int(_num(rec.get("Impressions")))
        slot["conversions"] += int(conv)
        for goal, names in cols.items():
            got = int(max(_num(rec.get(c)) for c in names))
            slot["goal_conversions"][goal] = slot["goal_conversions"].get(goal, 0) + got
    return list(agg.values())


def rule_raw(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Строки запросов -> вид отчёта площадок (direct.placements_window) для leads.build_rows:
    один путь расчёта лидов, CR и цены лида у обоих модулей. Объект — ключ "query"."""
    return [{"campaign_id": q["campaign_id"], "query": q["query"], "clicks": q["clicks"],
             "cost": q["cost"], "impressions": q["impressions"],
             "conversions": dict(q["goal_conversions"])} for q in rows]


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


class _Family:
    """Запросы одной кампании с индексом по словам: поток под минус-фразой.

    Индекс, а не перебор: у широкого правила пользователя кандидатов тысячи, и обход
    всех запросов кампании на каждого был бы квадратом. Ключ основы — первые STEM_MIN
    букв: _same_stem совпадает только при равенстве или при общем начале не короче
    STEM_MIN, так что индекс ничего не теряет.
    """

    def __init__(self, items: List[Dict[str, Any]]):
        self.items = items
        self.words = [_fold(_words(q["query"])) for q in items]
        self._exact: Dict[str, Set[int]] = {}
        self._stem: Dict[str, Set[int]] = {}
        for i, ws in enumerate(self.words):
            for w in ws:
                self._exact.setdefault(w, set()).add(i)
                self._stem.setdefault(w[:STEM_MIN], set()).add(i)

    def hits(self, words: FrozenSet[str], stems: bool) -> List[int]:
        if stems:
            if not words:
                return []
            pools = [self._stem.get(yo(w)[:STEM_MIN], set()) for w in words]
            return sorted(i for i in set.intersection(*pools)
                          if _covers(words, self.words[i]))
        folded = _fold(words)
        if not folded:
            return list(range(len(self.items)))
        return sorted(set.intersection(*(self._exact.get(w, set()) for w in folded)))

    def stats(self, words: FrozenSet[str], stems: bool) -> Dict[str, Any]:
        """stems=False — точные формы (ё=е): это видно в находке. stems=True — с грубыми
        словоформами: только для отказов, где лишнее совпадение снимает кандидата, а не
        добавляет минус. conversions None — лиды кампании неизвестны."""
        hit = [self.items[i] for i in self.hits(words, stems)]
        hit.sort(key=lambda q: -q["cost"])
        conv = [q["conversions"] for q in hit]
        return {"clicks": sum(q["clicks"] for q in hit),
                "cost": round(sum(q["cost"] for q in hit), 2),
                "impressions": sum(q.get("impressions") or 0 for q in hit),
                "conversions": None if None in conv else sum(conv),
                "queries_sample": [q["query"] for q in hit[:SAMPLE]]}


@dataclass
class _Scan:
    findings: List[Finding] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    # Строки — отказы встроенных (как в этапе 1), dict — отказы с кодом (reasons.refusal).
    refused: List[Any] = field(default_factory=list)
    over_cap: int = 0
    over: Dict[str, int] = field(default_factory=dict)
    # Отказы-строки встроенных по правилу: кода у них нет, а by_rule их считает.
    lines: Dict[str, int] = field(default_factory=dict)
    unverified: List[tuple] = field(default_factory=list)
    campaigns: int = 0
    objects: int = 0
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
    on = settings["builtin"]
    mult = float(settings["multiplier"])
    clicks = sum(q["clicks"] for q in rows)
    base = sum(q["conversions"] for q in rows) / clicks if clicks else 0.0
    out: List[Tuple[str, str, str]] = []
    if on["phrase_waste"]:
        phrase = objects.minus_word_candidates(objects.top_queries_by_cost(rows),
                                               cpa_limit=cpa_limit, multiplier=mult,
                                               base_conversion=base)
        if phrase:
            kept, _dropped = objects.phrases_cutting_only_waste(
                phrase, rows, cpa_limit=cpa_limit, multiplier=mult, bought=own_phrases)
            out += [("phrase_waste", c["query"], cid) for c in kept for cid in c["campaigns"]]
    if on["word_zero_conv"]:
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


def _rule_phrase(rule: R.Rule, row: R.Row) -> str:
    # Слово — из первого условия «содержит слово» (compile_rules без него правило
    # отвергает), но в написании запроса: ё=е — только для сравнения, а пишется то,
    # что человек увидит в запросе (как у minus_phrase и встроенных). Фраза — сам запрос.
    if rule.action not in R.WORD_ACTIONS:
        return row.object
    word = next(str(c.value) for c in rule.conditions
                if c.field == "query" and c.op == "has_word")
    return next((w for w in word_list(row.object) if yo(w) == yo(word.lower())), word)


@dataclass
class _Builtin:
    """Строки встроенных на логине: кампании с видимой целью и CPA, от которого судят."""
    by_cid: Dict[str, List[Dict[str, Any]]]
    rows: List[Dict[str, Any]]
    cpa: float
    cpa_auto: bool
    account: Dict[str, Optional[float]]


@dataclass
class _Cand:
    """Минус-фраза правил пользователя в кампании: одно действие — одна находка.

    Действует первое по списку правило, давшее эту фразу (его строка — самая дорогая
    из его совпадений); остальные, давшие ту же фразу, — «сработали также» в matched.
    """
    cid: str
    phrase: str
    rule: R.Rule
    row: R.Row
    matched: List[dict]
    fam: Dict[str, Any] = field(default_factory=dict)


class _Login:
    """Скан одного логина: встроенные этапа 1, затем правила пользователя, общий лимит."""

    def __init__(self, acc: DirectAccount, settings: dict, rules: List[R.Rule],
                 lead: List[int], lead_code: Optional[str], win: Tuple[str, str], log):
        self.acc = acc
        self.settings = settings
        self.win = win
        self.log = log
        self.protect = [r for r in rules if r.action == R.PROTECT]
        self.acting = [r for r in rules if r.action != R.PROTECT]
        self.rules = rules
        self.by_id = {r.id: r for r in rules}
        self.rank = {USER + r.id: i for i, r in enumerate(self.acting)}
        self.lead = lead
        self.lead_code = lead_code
        self.rub = (acc.currency or "RUB").upper() == "RUB"
        self.out = _Scan()
        self.live: Dict[str, dict] = {}
        self.spend: Dict[str, float] = {}
        self.families: Dict[str, _Family] = {}
        self.attach: Dict[Tuple[str, str], List[dict]] = {}
        self._held: Dict[Tuple[str, int], Optional[Tuple[str, str, Optional[str]]]] = {}
        self._said: Set[tuple] = set()
        self._unv: Set[tuple] = set()

    # --- мелочи -------------------------------------------------------------------

    def skip(self, cid: str, reason: str) -> None:
        self.out.skipped.append("%s %s (%s): %s" % (self.acc.login, cid,
                                                    self.live[cid].get("Name") or "", reason))

    def refuse_line(self, rule: str, cid: str, phrase: str, reason: str) -> None:
        self.out.refused.append("%s %s «%s»: %s" % (self.acc.login, cid, phrase, reason))
        self.out.lines[rule] = self.out.lines.get(rule, 0) + 1

    def refuse(self, item: dict) -> None:
        sig = tuple(sorted((k, str(v)) for k, v in item.items()))
        if sig not in self._said:
            self._said.add(sig)
            self.out.refused.append(item)

    def unverified(self, rule_key: str, cid: Optional[Any] = None) -> None:
        item = ((self.acc.login, rule_key) if cid is None
                else (self.acc.login, rule_key, int(cid)))
        if item not in self._unv:
            self._unv.add(item)
            self.out.unverified.append(item)

    def unknown(self, rule: R.Rule, row: R.Row) -> Tuple[str, bool]:
        """(код, на весь логин ли) — почему условия правила на строке не проверить."""
        if not row.money_known and {c.field for c in rule.conditions} & R.MONEY_FIELDS:
            return "non_rub_money", True
        if self.lead_code:
            return self.lead_code, True
        return "goal_not_visible", False

    def gnv(self, code: str, cid: Optional[Any]) -> dict:
        # Текст goal_not_visible — «0 лидов при расходе X»: расход кампании за окно.
        if code != "goal_not_visible" or cid is None:
            return {}
        return {"cost": round(self.spend.get(str(cid), 0.0), 2)}

    def _named_unfit(self, every: Dict[str, dict]) -> None:
        """Кампания из only правила, остановленная или не того типа, — отказ, а не тишина.

        Только кампании этого логина: only — список на весь проект, чужие в нём не наши."""
        for r in self.acting:
            for cid in sorted(r.only):
                c = every.get(str(cid))
                if c is None or str(cid) in self.live:
                    continue
                code = ("campaign_type_unsupported" if c.get("Type") not in TYPES
                        else "campaign_stopped")
                self.refuse(refusal(code, USER + r.id, login=self.acc.login, campaign_id=cid))

    def builtin_keys(self) -> List[str]:
        return [k for k in TOGGLED if self.settings["builtin"][k]] + ["stop_word"]

    # --- скан ---------------------------------------------------------------------

    def scan(self) -> _Scan:
        acc, out = self.acc, self.out
        if not self.rub and not self.acting:
            # Пороги и CPA модуля в рублях; тенге без конвертации дали бы чужой масштаб.
            return out.skip_account(acc.login, NON_RUB % acc.currency)
        every = {str(c["Id"]): c for c in _campaigns(acc)}
        self.live = {cid: c for cid, c in every.items()
                     if c.get("Type") in TYPES and c.get("State") in STATES}
        self._named_unfit(every)
        if not self.live:
            self.log("[%s] нечего чистить: ни одной запущенной текстовой кампании или ЕПК"
                     % acc.login)
            return out
        out.campaigns = len(self.live)
        # Почему встроенные на логине целиком не судятся (правила пользователя — судятся).
        off = None if self.rub else NON_RUB % acc.currency
        goals_by_cid = self._builtin_goals() if self.rub else {}
        report_goals, dropped = _report_goals(goals_by_cid, self.lead if self.acting else [])
        for cid in dropped:
            # Цель, не попавшая в отчёт, дала бы кампании «ноль конверсий» там, где их не спросили.
            self.skip(cid, "целей в кабинете больше %d, её цели не вошли в отчёт"
                      % MAX_REPORT_GOALS)
            self.refuse(refusal("report_goals_over_cap", None, login=acc.login,
                                campaign_id=int(cid)))
            # Встроенные кампанию не судили: их прошлые находки в ней reconcile не закрывает.
            for key in self.builtin_keys():
                self.unverified(key, cid)
            del goals_by_cid[cid]
        # Правилам пользователя нужны все запущенные кампании, не только с целью стратегии:
        # условие без лидов судится и там, а лиды считаются по целям-лидам.
        cids = set(goals_by_cid) | (set(self.live) if self.acting else set())
        if not cids:
            return out

        raw = _report(acc, self.win, report_goals, list(cids))
        if report_goals and raw and not any(_GOAL_COLUMN.match(k) for k in raw[0]):
            if not self.acting:
                return out.skip_account(acc.login, BLIND)
            off = off or BLIND
            # Лиды посчитать нечем: условия по ним не проверены, а не «0 лидов».
            self.lead, self.lead_code = [], "report_failed"
        rows = _query_rows(raw, goals_by_cid, keep=cids)
        out.objects = len({negatives.normalize_phrase(q["query"]) for q in rows})

        builtin: Optional[_Builtin] = None
        if off is None:
            builtin, off = self._builtin_rows(rows, goals_by_cid)
        if off is not None:
            if not self.acting:
                return out.skip_account(acc.login, off)
            # Встроенные тут не судили: их живые находки reconcile не закрывает.
            out.skipped.append("%s: %s" % (acc.login, off))
            for key in self.builtin_keys():
                self.unverified(key)
        cands = self._user_candidates(rows) if self.rules else {}

        need = set(builtin.by_cid if builtin else ()) | {cid for cid, _p in cands}
        if not need:
            return out
        # Снимок ключей каждый прогон: matched_key отчёта — в основном подбор автотаргетинга
        # (замер агента 29.08: 1,9 % совпадений оказались ключами кабинета).
        keywords = direct_api.keywords_by_campaign(acc.token, acc.login,
                                                   [int(c) for c in sorted(need, key=int)])
        for cid in need:
            # Кампания без ключей (только автотаргетинг) защищать законно нечего; без этого
            # own_semantics откатилась бы на matched_key отчёта.
            keywords.setdefault(cid, [])
        never = _never_list(self.settings)
        # Запросы до уже стоящей минус-фразы ещё лежат в окне: покрытое ею второй раз не ставим.
        covered = {cid: [w for w in (_words(p) for p in (self.live[cid].get(FIELD) or {})
                                     .get("Items") or []) if w]
                   for cid in need}
        picked = self._builtin_picks(builtin, keywords, never, covered) if builtin else {}
        users = self._user_checks(cands, picked, keywords, never, covered)
        self._select(picked, users, builtin)
        return out

    # --- встроенные ---------------------------------------------------------------

    def _builtin_goals(self) -> Dict[str, List[int]]:
        goals_by_cid: Dict[str, List[int]] = {}
        for cid, c in self.live.items():
            goals = self.lead or strategy_goals(c)
            if goals:
                goals_by_cid[cid] = goals
            else:
                self.skip(cid, "судить без конверсий нельзя: у стратегии нет цели, "
                               "lead_goal_ids пуст")
        return goals_by_cid

    def _builtin_rows(self, rows: List[Dict[str, Any]], goals_by_cid: Dict[str, List[int]]
                      ) -> Tuple[Optional[_Builtin], Optional[str]]:
        """(строки встроенных или None — судить им нечего, причина пропуска логина)."""
        by_cid: Dict[str, List[Dict[str, Any]]] = {}
        for q in rows:
            if q["campaign_id"] in goals_by_cid:
                by_cid.setdefault(q["campaign_id"], []).append(q)
        for cid in sorted(by_cid, key=int):
            qs = by_cid[cid]
            if sum(q["cost"] for q in qs) > 0 and sum(q["conversions"] for q in qs) == 0:
                # Ноль по цели во ВСЕЙ кампании — цель не привязана к её счётчику, а не мусор.
                self.skip(cid, "цель не дала конверсий за окно — ноль конверсий здесь ничего "
                               "не значит")
                del by_cid[cid]
        mine = [q for qs in by_cid.values() for q in qs]
        if not mine:
            return None, None
        cost = sum(q["cost"] for q in mine)
        conv = sum(q["conversions"] for q in mine)
        clicks = sum(q["clicks"] for q in mine)
        cpa_limit = self.settings.get("cpa_limit")
        auto = not cpa_limit or cpa_limit <= 0
        if auto:
            if not conv:
                return None, NO_CPA
            cpa_limit = cost / conv
        return _Builtin(by_cid=by_cid, rows=mine, cpa=float(cpa_limit), cpa_auto=auto,
                        account={"cr": leads.pct(conv, clicks), "cpl": leads.div(cost, conv)}), None

    def _builtin_picks(self, b: _Builtin, keywords, never, covered
                       ) -> Dict[Tuple[str, str], Tuple[str, Dict[str, Any]]]:
        own_words, own_phrases = objects.own_semantics(
            b.rows, {cid: keywords[cid] for cid in b.by_cid})
        fams = {cid: _Family(qs) for cid, qs in b.by_cid.items()}
        payoff = b.cpa * float(self.settings["multiplier"])
        picked: Dict[Tuple[str, str], Tuple[str, Dict[str, Any]]] = {}
        seen = set()  # (кампания, фраза с ё=е): «учёба» и «учеба» — одна запись в Директе
        for rule, text, cid in _candidates(b.rows, self.settings, b.cpa, own_words,
                                           own_phrases):
            phrase = negatives.normalize_phrase(text)
            if (cid, yo(phrase)) in seen or cid not in b.by_cid:
                continue
            ok, why = negatives.phrase_is_valid(phrase)
            words = _words(phrase)
            if not ok:
                self.refuse_line(rule, cid, phrase, why)
                continue
            guard = self._guard(cid, phrase, words, rule)
            if guard is not None and guard["code"] == "user_protect":
                self.guarded(guard)
                continue
            if _never_hit(words, never) is not None:
                self.refuse_line(rule, cid, phrase, "в списке never_minus")
                continue
            if guard is not None:
                self.guarded(guard)
                continue
            if any(_covers(w, words) for w in covered[cid]):
                continue
            if _key_conflict(words, own_phrases.get(cid, ())):
                # Отказ при применении всё равно случился бы: находку, обречённую на failed, не ставим.
                self.refuse_line(rule, cid, phrase, "конфликт с ключом")
                continue
            fam = fams[cid].stats(words, stems=False)
            if fam["clicks"] <= 0:
                continue
            wide = fams[cid].stats(words, stems=True)
            if rule == "word_zero_conv" and wide["conversions"] > 0:
                # Агент судил слово в точной форме; Директ отрежет и формы, а у них конверсии есть.
                self.refuse_line(rule, cid, phrase, "словоформы слова приносят конверсии")
                continue
            paying = [f for f in (fam, wide)
                      if f["conversions"] > 0 and f["cost"] / f["conversions"] <= payoff]
            if rule == "phrase_waste" and paying:
                # Семейство агент проверял по всему кабинету; запрет пишется в кампанию, и там,
                # где поток окупается (в точных формах или со словоформами), ему не место.
                self.refuse_line(rule, cid, phrase, "в этой кампании семейство окупается "
                                 "(CPA %.0f ₽)" % (paying[0]["cost"] / paying[0]["conversions"]))
                continue
            picked[(cid, phrase)] = (rule, fam)
            seen.add((cid, yo(phrase)))
        return picked

    def _builtin_params(self, rule: str, b: _Builtin) -> dict:
        if rule == "stop_word":
            return {}
        return {"cpa_limit": round(b.cpa, 2), "cpa_auto": b.cpa_auto,
                "multiplier": float(self.settings["multiplier"])}

    # --- защиты -------------------------------------------------------------------

    def _protect_state(self, cid: str, i: int) -> Optional[Tuple[str, str, Optional[str]]]:
        """("protect", id, None) — запрос защищён; ("held", id, код) — защиту не проверить."""
        memo = (cid, i)
        if memo not in self._held:
            row = self.families[cid].items[i]["row"]
            m = R.evaluate(self.protect, row)
            state = None
            if m is not None and m.rule is not None:
                state = ("protect", m.rule.id, None)
            elif m is not None and m.held_by is not None:
                state = ("held", m.held_by, self.unknown(self.by_id[m.held_by], row)[0])
            self._held[memo] = state
        return self._held[memo]

    def _guard(self, cid: str, phrase: str, words: FrozenSet[str], key: str
               ) -> Optional[dict]:
        """Отказ защиты пользователя для минус-фразы в кампании или None.

        Защищённым считается запрос, который фраза отрезала бы в любой словоформе: лишнее
        совпадение только отказывает. Сработавшая защита сильнее непроверенной.
        """
        fam = self.families.get(cid)
        if not self.protect or fam is None:
            return None
        held = None
        for i in fam.hits(words, stems=True):
            state = self._protect_state(cid, i)
            if state is None:
                continue
            kind, by, code = state
            if kind == "protect":
                return refusal("user_protect", key, login=self.acc.login, campaign_id=int(cid),
                               obj=phrase, by=USER + by)
            if held is None:
                held = refusal(code, key, login=self.acc.login, campaign_id=int(cid),
                               obj=phrase, by=USER + by, held=True, **self.gnv(code, cid))
        return held

    def guarded(self, item: dict) -> None:
        if item.get("held"):
            # Находка этого правила тут не проверена: прошлую reconcile закрывать не должен.
            self.unverified(item["rule"], item["campaign_id"])
        self.refuse(item)

    # --- правила пользователя -----------------------------------------------------

    def _login_wide(self) -> None:
        # Причина на весь логин видна и без строк: правило без целей-лидов не проверит
        # лиды нигде, и человек должен это увидеть, даже если запросов за окно нет.
        for r in self.rules:
            fields = {c.field for c in r.conditions}
            code = None
            if fields & R.MONEY_FIELDS and not self.rub:
                code = "non_rub_money"
            elif fields & R.LEAD_FIELDS and self.lead_code:
                code = self.lead_code
            if code is None:
                continue
            self.refuse(refusal(code, USER + r.id, login=self.acc.login))
            if r.action != R.PROTECT:
                self.unverified(USER + r.id)

    def _user_candidates(self, rows: List[Dict[str, Any]]) -> Dict[Tuple[str, str], _Cand]:
        urows, _invisible = leads.build_rows(rule_raw(rows), goals=self.lead, by="campaign",
                                             currency=self.acc.currency,
                                             normalize=negatives.normalize_phrase, key="query")
        by: Dict[str, List[Dict[str, Any]]] = {}
        for r in urows:
            m = r.metrics
            by.setdefault(str(r.campaign_id), []).append(
                {"query": r.object, "clicks": m["clicks"], "cost": m["cost"],
                 "impressions": m["impressions"], "conversions": m["leads"], "row": r})
        self.families = {cid: _Family(items) for cid, items in by.items()}
        self.spend = {cid: sum(q["cost"] or 0.0 for q in items) for cid, items in by.items()}
        self._login_wide()
        login = self.acc.login
        cands: Dict[Tuple[str, str], _Cand] = {}
        for row in urows if self.acting else ():
            m = R.evaluate(self.acting, row)
            if m is None:
                continue
            for rid in m.unverified:
                code, whole = self.unknown(self.by_id[rid], row)
                cid = None if whole else row.campaign_id
                self.unverified(USER + rid, cid)
                self.refuse(refusal(code, USER + rid, login=login, campaign_id=cid,
                                    **self.gnv(code, cid)))
            # Строки идут по убыванию расхода: первая строка правила — его самая дорогая.
            for hit in m.matched:
                rule = self.by_id[hit["id"]]
                phrase = negatives.normalize_phrase(_rule_phrase(rule, row))
                entry = {"id": USER + rule.id, "name": rule.name, "conds": hit["conds"]}
                key = (str(row.campaign_id), yo(phrase))
                c = cands.get(key)
                if c is None:
                    cands[key] = _Cand(cid=key[0], phrase=phrase, rule=rule, row=row,
                                       matched=[entry])
                elif all(x["id"] != entry["id"] for x in c.matched):
                    c.matched.append(entry)
                    c.matched.sort(key=lambda x: self.rank[x["id"]])
                    if self.rank[entry["id"]] < self.rank[USER + c.rule.id]:
                        c.rule, c.row, c.phrase = rule, row, phrase
        return cands

    def _user_checks(self, cands, picked, keywords, never, covered) -> List[_Cand]:
        login = self.acc.login
        _w, own_phrases = objects.own_semantics(
            [], {cid: keywords[cid] for cid in {k[0] for k in cands}})
        builtin = {(cid, yo(p)): (cid, p) for cid, p in picked}
        ok: List[_Cand] = []
        for key in sorted(cands, key=lambda k: (int(k[0]), k[1])):
            c = cands[key]
            if key in builtin:
                # Встроенное раньше по порядку: действие одно, совпавшие — «сработали также».
                self.attach.setdefault(builtin[key], []).extend(c.matched)
                continue
            rule_key, cid, phrase = USER + c.rule.id, c.cid, c.phrase

            def no(code, **extra):
                self.refuse(refusal(code, rule_key, login=login, campaign_id=int(cid),
                                    obj=phrase, **extra))

            valid, why = negatives.phrase_is_valid(phrase)
            if not valid:
                # Спецсимволы, длина слова и фразы: запись упала бы, отказ на скане виден сразу.
                no("phrase_invalid", detail=why)
                continue
            words = _words(phrase)
            guard = self._guard(cid, phrase, words, rule_key)
            if guard is not None and guard["code"] == "user_protect":
                self.guarded(guard)
                continue
            if _never_hit(words, never) is not None:
                no("in_never_minus")
                continue
            if guard is not None:
                self.guarded(guard)
                continue
            if any(_covers(w, words) for w in covered[cid]):
                no("covered_by_minus")
                continue
            if _key_conflict(words, own_phrases.get(cid, ())):
                no("conflicts_keyword")
                continue
            if c.rule.action == "minus_word":
                # Условия правила — по одной строке, а минус-слово режет все запросы кампании
                # с этим словом и его формами (урок «высшее» 25.08). Слово, которое где-то
                # в кампании приносит лиды, не минусуем; лиды неизвестны — не проверено.
                wide = self.families[cid].stats(words, stems=True)
                if wide["conversions"] is None:
                    code = self.lead_code or "goal_not_visible"
                    self.unverified(rule_key, cid)
                    no(code, **self.gnv(code, cid))
                    continue
                if wide["conversions"] > 0:
                    no("family_pays_off", leads=wide["conversions"])
                    continue
            c.fam = self.families[cid].stats(words, stems=False)
            if c.fam["clicks"] <= 0:
                continue
            ok.append(c)
        return ok

    # --- лимит и находки ----------------------------------------------------------

    def _select(self, picked, users: List[_Cand], b: Optional[_Builtin]) -> None:
        """Встроенные берут лимит первыми (как в этапе 1, по расходу), остаток — правилам
        пользователя по расходу их потока. 10 на кампанию — общие."""
        out = self.out
        cap = int(self.settings["max_per_run"])
        per_cid: Dict[str, int] = {}

        def room(cid: str, key: str) -> bool:
            if len(out.findings) >= cap or per_cid.get(cid, 0) >= PER_CAMPAIGN:
                out.over_cap += 1
                out.over[key] = out.over.get(key, 0) + 1
                return False
            per_cid[cid] = per_cid.get(cid, 0) + 1
            return True

        for (cid, phrase), (rule, fam) in sorted(picked.items(),
                                                 key=lambda kv: (-kv[1][1]["cost"], kv[0])):
            if room(cid, rule):
                out.findings.append(self._builtin_finding(cid, phrase, rule, fam, b))
        for c in sorted(users, key=lambda x: (-x.fam["cost"], int(x.cid), x.phrase)):
            if room(c.cid, USER + c.rule.id):
                out.findings.append(self._user_finding(c))

    def _finding(self, rule: str, cid: str, phrase: str, details: dict) -> Finding:
        what = "Минус-слово" if len(_words(phrase)) == 1 else "Минус-фраза"
        return Finding(
            rule=rule, severity="info",
            title="%s «%s» в кампании %s" % (what, phrase, cid),
            login=self.acc.login, campaign_id=int(cid),
            campaign_name=self.live[cid].get("Name") or "",
            object_type="phrase", object_id=phrase, details=details,
            action={"op": "add_negative", "campaign_id": int(cid), "phrase": phrase})

    def _base_details(self, fam: Dict[str, Any]) -> dict:
        return {"clicks": fam["clicks"], "cost": fam["cost"], "conversions": fam["conversions"],
                "window_from": self.win[0], "window_to": self.win[1],
                "queries_sample": fam["queries_sample"],
                "window": {"from": self.win[0], "to": self.win[1]}}

    def _builtin_finding(self, cid, phrase, rule, fam, b: _Builtin) -> Finding:
        name = BUILTIN_NAMES[rule]
        conv = fam["conversions"]
        details = self._base_details(fam)
        details.update(
            rule={"id": rule, "name": name, "builtin": True,
                  "params": self._builtin_params(rule, b)},
            matched=[{"id": rule, "name": name, "conds": []}]
            + self.attach.get((cid, phrase), []),
            # Лиды встроенных — как в этапе 1: максимум по целям кампании.
            metrics=leads.finding_metrics(
                {"clicks": fam["clicks"], "cost": fam["cost"],
                 "impressions": fam["impressions"], "leads": conv,
                 "cr": leads.pct(conv, fam["clicks"]), "cpl": leads.div(fam["cost"], conv)},
                b.account))
        return self._finding(rule, cid, phrase, details)

    def _user_finding(self, c: _Cand) -> Finding:
        key = USER + c.rule.id
        details = self._base_details(c.fam)
        details.update(rule=dict(R.snapshot(c.rule), id=key), matched=c.matched,
                       metrics=leads.finding_metrics(c.row.metrics, c.row.account),
                       # Строка, на которой сработало правило: минус-слово — не сам запрос.
                       query=c.row.object)
        if c.rule.action in PROPOSE:
            # Авторежим такие не одобряет (store.approve_auto): решение за человеком.
            details["propose"] = True
        return self._finding(key, c.cid, c.phrase, details)


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


def _silent_keys(settings: dict, rules: List[R.Rule]) -> List[str]:
    """Правила, которые прогон намеренно не проверял: выключенные встроенные, выключенные
    и невалидные правила пользователя. Их живые находки reconcile не закрывает: выключить
    правило — не значит признать его находки исправленными."""
    keys = [k for k in TOGGLED if not settings["builtin"][k]]
    active = {r.id for r in rules}
    for item in settings.get("rules") or []:
        rid = item.get("id") if isinstance(item, dict) else None
        if isinstance(rid, str) and rid and rid not in active and USER + rid not in keys:
            keys.append(USER + rid)
    return keys


def run(ctx: Context) -> RunOutput:
    settings, notes = checked_settings(ctx.settings)
    win = window(ctx.now, settings["window_days"])
    rules, rule_notes = R.compile_rules(KEY, settings.get("rules") or [])
    lead, lead_code = leads.lead_goals(settings, ctx.project_goals)
    silent = _silent_keys(settings, rules)
    findings: List[Finding] = []
    skipped: List[str] = []
    refused: List[Any] = [refusal("rule_invalid",
                                  USER + n["rule_id"] if n.get("rule_id") else None,
                                  detail=n.get("detail")) for n in rule_notes]
    if lead_code == "too_many_goals":
        # Больше 10 целей-лидов приходит только из ролей целей проекта (форма модуля не
        # пустит): встроенные тогда судят по целям стратегий, и это должно быть видно.
        refused.append(refusal("too_many_goals", None))
    unverified: List[tuple] = []
    scanned: List[str] = []
    errors: List[str] = []
    over_cap = 0
    over: Dict[str, int] = {}
    lines: Dict[str, int] = {}
    read = {"logins": 0, "campaigns": 0, "objects": 0}
    total = len(ctx.accounts)
    for i, acc in enumerate(ctx.accounts, 1):
        try:
            scan = _Login(acc, settings, rules, lead, lead_code, win, ctx.log).scan()
        except Exception as err:  # кабинет не должен ронять остальные
            errors.append("%s: %s" % (acc.login, err))
            ctx.log("[%s] ошибка: %s" % (acc.login, err))
        else:
            read["logins"] += 1
            read["campaigns"] += scan.campaigns
            read["objects"] += scan.objects
            skipped += scan.skipped
            refused += scan.refused
            over_cap += scan.over_cap
            for src, dst in ((scan.over, over), (scan.lines, lines)):
                for key, n in src.items():
                    dst[key] = dst.get(key, 0) + n
            unverified += scan.unverified + [(acc.login, k) for k in silent]
            if scan.judged:
                findings += scan.findings
                scanned.append(acc.login)
        ctx.progress({"step": acc.login, "logins_done": i, "logins_total": total})
    if errors and not read["logins"]:
        raise RuntimeError("ни один логин не прочитан: " + "; ".join(errors))
    # over_cap — общий счёт, как в этапе 1; по правилам — в by_rule.
    summary: Dict[str, Any] = {"window": list(win), "skipped": skipped, "refused": refused,
                               "over_cap": over_cap, "read": read,
                               "by_rule": by_rule([USER + r.id for r in rules], findings, refused,
                                                  over, lines)}
    if silent:
        # Те же ключи лежат в unverified каждого логина: по этому списку UI отличает
        # «выключено настройкой» от «не проверено».
        summary["disabled"] = silent
    if errors:
        summary["errors"] = errors
    if notes:
        summary["settings_notes"] = notes
    # Упавший и пропущенный целиком логин не судили: его открытые находки не закрываем.
    return RunOutput(findings=findings, summary=summary, scanned_logins=scanned,
                     unverified=unverified)


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
