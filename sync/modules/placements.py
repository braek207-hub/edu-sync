# -*- coding: utf-8 -*-
"""Модуль placements: чистка мусорных площадок РСЯ на движке модулей.

Порядок решений по площадке в кампании (первое сработавшее даёт находку):
1. защиты — пользовательский protect, «Никогда не закрывать», системный белый список;
2. «Всегда закрывать»; 3. словарь (включённые категории, ворота top_n); 4. спам;
5. пользовательские правила в порядке списка.

Встроенные (2–4) считает прежний sync.placements.plan: правила, ворота и fingerprint
у них те же, что в этапе 1, — к ним привязана история и перенесённые запреты.
Пользовательские правила судят строки отчётов за свои окна (leads.build_rows) и идут
под общий лимит max_per_run. Запись и откат — по правилу движка: свежее чтение ->
слияние -> запись -> чтение обратно.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from sync.modules import direct_api, leads
from sync.modules import rules as R
from sync.modules.accounts import DirectAccount
from sync.modules.reasons import by_rule, refusal
from sync.modules.queries import MSK
from sync.modules.types import ApplyResult, Context, Finding, RunOutput, StoredFinding
from sync.placements import classify, direct, journal, llm, spam
from sync.placements import plan as planner

KEY = "placements"
FIELD = "ExcludedSites"
# Платный второй судья по умолчанию выключен, как и в старом CLI (там был --llm).
LLM_ENV = "PLACEMENTS_LLM"
# Префикс правила пользователя в fingerprint и сводке: id правила не должен совпасть
# с именем встроенного (app, spam…), иначе они делили бы одну историю находок.
USER = "u:"
DICTIONARY = ("app", "dsp", "game", "junk")
# Эти вердикты режутся всегда: второй судья, «Всегда закрывать», спам. Настройка
# dictionary.categories включает и выключает только категории словаря.
PINNED_VERDICTS = frozenset({"llm", "forced", "spam"})
BUILTIN_NAMES = {"app": "Словарь мусора", "dsp": "Словарь мусора", "game": "Словарь мусора",
                 "junk": "Словарь мусора", "llm": "Второй судья", "forced": "Всегда закрывать",
                 "spam": "Спам-площадки"}


def _site(name) -> str:
    return classify.normalize(name)


def _system_allowed(site: str) -> bool:
    # Тот же белый список, что у спам-правила (spam.py): известные приложения,
    # неймспейсы издателей и крупные порталы.
    return (site in classify.ALLOW_EXACT or llm.is_protected(site)
            or any(site.startswith(p) for p in classify.ALLOW_PREFIX))


def _window(now: datetime, days: int) -> dict:
    """Даты окна по Москве: 0 — сегодня (отчёт TODAY), 7/30 — LAST_N_DAYS Директа,
    который сегодняшний день не включает."""
    today = now.astimezone(MSK).date()
    if not days:
        return {"from": today.isoformat(), "to": today.isoformat()}
    to = today - timedelta(days=1)
    return {"from": (to - timedelta(days=days - 1)).isoformat(), "to": to.isoformat()}


def _metrics(row: Optional[R.Row]) -> dict:
    # Строки площадки в окне встроенного может не быть: метрики тогда нулевые, а не пропуск.
    if row is None:
        return leads.finding_metrics({}, {})
    return leads.finding_metrics(row.metrics, row.account)


def ask_model(plan, ask, use_db: bool, log):
    """Второй судья по кандидатам плана: (поправки, строка отчёта, модель не ответила).

    Модель видит только имена в окне ворот, которые словарь оставил обычными
    сайтами и о которых её ещё не спрашивали: вердикт кэшируется навсегда,
    потому что домен не меняет природу. Потолок ASK_LIMIT сдерживает первые
    прогоны — остаток достаётся следующему такту.
    """
    candidates = plan.get("candidates") or []
    if not candidates:
        return {}, None, False

    cached = {}
    if use_db:
        try:
            cached = journal.load_llm_verdicts(candidates)
        except Exception as err:
            log("  кэш вердиктов недоступен: %s" % err)

    unknown = [s for s in candidates if s not in cached][:llm.ASK_LIMIT]
    errors: List[str] = []
    fresh = llm.judge(unknown, ask=ask, errors=errors)
    if fresh and use_db:
        try:
            journal.save_llm_verdicts(fresh, llm.model_name(ask))
        except Exception as err:
            log("  вердикты модели не сохранились: %s" % err)

    overrides = llm.overrides_from(dict(cached, **fresh))
    note = ("  модель: кандидатов %d, из кэша %d, спрошено %d, ответов %d, "
            "к запрету %d"
            % (len(candidates), len(cached), len(unknown), len(fresh),
               len(overrides)))
    if errors:
        # Без этой строки мёртвый провайдер неотличим от чистого кабинета.
        note += ("\n  МОДЕЛЬ НЕ ОТВЕТИЛА (%d раз): %s" % (len(errors), errors[0]))
    return overrides, note, bool(errors)


def _refusal_line(login: str, item: dict) -> str:
    who = item.get("name") or item.get("campaign_id") or ""
    if item.get("placement"):
        who = "%s / %s" % (who, item["placement"])
    line = "%s %s: %s" % (login, who, item.get("reason"))
    if item.get("clicks"):
        # Деньги в отказе показываем всегда: недостижимая кампания молча
        # съедает бюджет, и без цифры это рядовая техническая строка.
        line += " [%d кл, %.0f ₽]" % (item["clicks"], item.get("cost") or 0)
    return line


@dataclass
class _Frame:
    """Строки одного окна логина: площадка × кампания и площадка × логин."""
    seen: Dict[Tuple[int, str], R.Row]
    account: Dict[str, R.Row]
    invisible: Dict[int, str]
    base: Dict[str, Optional[float]]
    money_known: bool
    has_goals: bool
    # Расход кампании за окно по всем её площадкам; ключи — кампании, что есть в отчёте.
    cost_by_campaign: Dict[int, float]

    def empty(self, site: str, cid: int, scope: str, category: str) -> R.Row:
        # Площадки нет в прочитанном окне: ноль кликов и ноль лидов — факт, а не
        # «неизвестно». Не видна цель кампании — лиды и тут неизвестны.
        known = self.has_goals and (scope == "account" or cid not in self.invisible)
        return R.Row(object=site,
                     campaign_id=cid if scope == "campaign" else leads.ACCOUNT_CAMPAIGN_ID,
                     category=category,
                     metrics={"clicks": 0, "cost": 0.0, "impressions": 0, "ctr": None,
                              "leads": 0 if known else None, "cr": None, "cpl": None},
                     account=dict(self.base), leads_known=known,
                     money_known=self.money_known, scope=scope)

    def spend(self, cid: Optional[int]) -> float:
        """Расход за окно для текста «0 лидов при расходе X» у goal_not_visible:
        кампании cid, а для строки по логину (None) — кампаний с невидимой целью:
        лиды неизвестны из-за них, а не из-за всего логина."""
        cids = self.invisible if cid is None else (cid,)
        return leads.r2(sum(self.cost_by_campaign.get(c, 0.0) for c in cids))


def _frame(raw: List[Dict[str, Any]], goals: List[int], currency: str,
           category: Callable[[str], str]) -> _Frame:
    seen, invisible = leads.build_rows(raw, goals=goals, by="campaign", currency=currency,
                                       normalize=_site)
    acct, _ = leads.build_rows(raw, goals=goals, by="account", currency=currency,
                               normalize=_site)
    for row in seen + acct:
        row.category = category(row.object)
    base = dict(seen[0].account) if seen else {"cr": None, "cpl": None}
    cost_by: Dict[int, float] = {}
    for r in seen:
        cost_by[r.campaign_id] = cost_by.get(r.campaign_id, 0.0) + (r.metrics.get("cost") or 0.0)
    return _Frame(seen={(r.campaign_id, r.object): r for r in seen},
                  account={r.object: r for r in acct}, invisible=invisible, base=base,
                  money_known=(currency or "RUB").upper() == "RUB", has_goals=bool(goals),
                  cost_by_campaign=cost_by)


@dataclass
class _Candidate:
    """Совпадение пользовательского правила, прошедшее защиты: ждёт лимита прогона."""
    login: str
    campaign: dict
    site: str
    rule: R.Rule
    row: R.Row
    matched: List[dict]
    list_before: int
    list_limit: int

    @property
    def campaign_id(self) -> int:
        return int(self.campaign["Id"])

    @property
    def cost(self) -> float:
        return float(self.row.metrics.get("cost") or 0.0)


@dataclass
class _LoginScan:
    findings: List[Finding] = field(default_factory=list)
    candidates: List[_Candidate] = field(default_factory=list)
    # Строки — отказы плана встроенных (как в этапе 1), dict — отказы с кодом.
    refused: List[Any] = field(default_factory=list)
    unverified: List[tuple] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    room: Dict[Tuple[str, int], int] = field(default_factory=dict)
    campaigns: int = 0
    objects: int = 0


class _Login:
    """Скан одного логина: встроенные через план этапа 1, затем правила пользователя."""

    def __init__(self, acc: DirectAccount, settings: dict, rules: List[R.Rule],
                 goals: List[int], goals_code: Optional[str], now: datetime, log):
        self.acc = acc
        self.settings = settings
        self.rules = rules
        self.protect = [r for r in rules if r.action == R.PROTECT]
        self.goals = goals
        self.goals_code = goals_code
        self.now = now
        self.log = log
        self.extra = {_site(s) for s in settings["extra_allow"] if _site(s)}
        self.money_known = (acc.currency or "RUB").upper() == "RUB"
        self.out = _LoginScan()
        self.frames: Dict[int, _Frame] = {}
        self.by_id: Dict[int, dict] = {}
        self.builtin: Dict[Tuple[int, str], Finding] = {}
        self._reports: Dict[Tuple[int, Tuple[int, ...]], list] = {}
        self._cats: Dict[str, str] = {}
        self._guards: Dict[Tuple[str, int, bool], Optional[dict]] = {}
        self._existing: Dict[int, Set[str]] = {}
        self._said: Set[tuple] = set()
        self._unv: Set[tuple] = set()

    # --- мелочи -------------------------------------------------------------------

    def category(self, site: str) -> str:
        if site not in self._cats:
            self._cats[site] = classify.classify(site)[0]
        return self._cats[site]

    def report(self, days: int, goals: List[int]) -> list:
        # Один отчёт на (окно, цели): спам по целям-лидам и правила за 7 дней с теми же
        # целями — один заказ в Директе, а не два.
        key = (days, tuple(sorted(goals)))
        if key not in self._reports:
            self._reports[key] = direct.placements_window(self.acc.token, self.acc.login,
                                                          days, list(key[1]))
        return self._reports[key]

    def existing(self, campaign: dict) -> Set[str]:
        cid = int(campaign["Id"])
        if cid not in self._existing:
            items = (campaign.get(FIELD) or {}).get("Items") or []
            self._existing[cid] = {s for s in (_site(x) for x in items) if s}
        return self._existing[cid]

    def refuse(self, item: dict) -> None:
        sig = tuple(sorted((k, str(v)) for k, v in item.items()))
        if sig not in self._said:
            self._said.add(sig)
            self.out.refused.append(item)

    def unverified(self, rule_key: str, cid: Optional[int] = None) -> None:
        item = ((self.acc.login, rule_key) if cid is None
                else (self.acc.login, rule_key, int(cid)))
        if item not in self._unv:
            self._unv.add(item)
            self.out.unverified.append(item)

    def unknown(self, rule: R.Rule, row: R.Row) -> Tuple[str, bool]:
        """(код, на весь логин ли) — почему условия правила на строке не проверить."""
        fields = {c.field for c in rule.conditions}
        if not row.money_known and fields & R.MONEY_FIELDS:
            return "non_rub_money", True
        if self.goals_code:
            return self.goals_code, True
        return "goal_not_visible", row.scope == "account"

    # --- защиты -------------------------------------------------------------------

    def rule_row(self, rule: R.Rule, site: str, cid: int) -> Optional[R.Row]:
        """Строка, на которой правило судит площадку в кампании cid; None — кампания
        вне правила."""
        frame = self.frames[rule.window]
        if rule.where == "account":
            # Строка по логину кампаний не знает: only/except применяются к кампании,
            # которую собираются закрыть.
            if (rule.only and cid not in rule.only) or cid in rule.except_:
                return None
            return frame.account.get(site) or frame.empty(site, cid, "account",
                                                          self.category(site))
        return frame.seen.get((cid, site)) or frame.empty(site, cid, "campaign",
                                                          self.category(site))

    def guard(self, site: str, cid: int, rule_key: str, system: bool) -> Optional[dict]:
        """Отказ защиты для действия rule_key над площадкой в кампании или None.

        system=False — у встроенных: «Всегда закрывать» сильнее системного белого
        списка (явное действие человека), а словарь и спам сверяются с ним сами.
        """
        memo = (site, cid, system)
        if memo not in self._guards:
            self._guards[memo] = self._guard(site, cid, system)
        found = self._guards[memo]
        return None if found is None else dict(found, rule=rule_key)

    def _guard(self, site: str, cid: int, system: bool) -> Optional[dict]:
        login = self.acc.login
        held = None
        for rule in self.protect:
            row = self.rule_row(rule, site, cid)
            if row is None:
                continue
            m = R.evaluate([rule], row)
            if m is None:
                continue
            if m.rule is not None:
                return refusal("user_protect", None, login=login, campaign_id=cid, obj=site,
                               by=USER + rule.id)
            # Защиту не проверить — площадка держится: закрыть, не зная, защищена ли
            # она, нельзя. Сработавшая защита ниже по списку решает и без неё.
            if held is None:
                code = self.unknown(rule, row)[0]
                extra = ({"cost": leads.r2(row.metrics.get("cost") or 0.0)}
                         if code == "goal_not_visible" else {})
                held = refusal(code, None, login=login, campaign_id=cid, obj=site,
                               by=USER + rule.id, held=True, **extra)
        # Твёрдые защиты раньше удержания: площадка из списка «никогда» защищена при
        # любых лидах, и лишняя тройка unverified держала бы её старую находку открытой.
        if site in self.extra:
            return refusal("in_never_list", None, login=login, campaign_id=cid, obj=site)
        if system and _system_allowed(site):
            return refusal("system_allowlist", None, login=login, campaign_id=cid, obj=site)
        return held

    def guarded(self, item: dict) -> None:
        if item.get("held"):
            # Находка этого правила тут не проверена: прошлую reconcile закрывать не должен.
            self.unverified(item["rule"], item["campaign_id"])
        self.refuse(item)

    # --- скан ---------------------------------------------------------------------

    def scan(self, ask, use_db: bool) -> _LoginScan:
        acc, out = self.acc, self.out
        campaigns = direct.campaigns(acc.token, acc.login)
        live = [c for c in campaigns
                if c.get("Type") in direct.CLEANABLE_TYPES
                and c.get("State") in direct.CLEANABLE_STATES]
        if not live:
            # У агентского кабинета половина клиентов пустая или выключена:
            # заказывать по ним отчёт — минуты прогона впустую.
            self.log("[%s] нечего чистить: ни одной запущенной кампании с рычагом" % acc.login)
            return out
        out.campaigns = len(live)
        self.by_id = {int(c["Id"]): c for c in campaigns}

        rows = direct.placements_today(acc.token, acc.login)
        self.frames[0] = _frame(rows, [], acc.currency, self.category)
        self._read_windows()
        objects = {_site(r.get("placement")) for r in rows}
        for raw in self._reports.values():
            objects |= {_site(r.get("placement")) for r in raw}
        out.objects = len(objects - {""})

        self._login_wide_refusals()
        plan = self._plan(rows, campaigns, ask, use_db)
        self._builtin_findings(plan, live)
        self._user(live)
        return out

    def _read_windows(self) -> None:
        for days in sorted({r.window for r in self.rules} - {0}):
            try:
                raw = self.report(days, self.goals)
            except Exception as err:
                if any(r.window == days for r in self.protect):
                    # Не зная, что защищено, закрывать нельзя ничего: логин целиком в ошибки.
                    raise RuntimeError("отчёт за %d дн. для защит правил: %s" % (days, err))
                self.out.problems.append("правила, отчёт за %d дн.: %s" % (days, err))
                for r in self.rules:
                    if r.window == days:
                        self.refuse(refusal("report_failed", USER + r.id, login=self.acc.login))
                continue
            self.frames[days] = _frame(raw, self.goals, self.acc.currency, self.category)

    def _login_wide_refusals(self) -> None:
        # Причина на весь логин видна и без строк: правило без целей-лидов не проверит
        # лиды нигде, и человек должен это увидеть, даже если площадок за окно нет.
        for r in self.rules:
            fields = {c.field for c in r.conditions}
            code = None
            if fields & R.MONEY_FIELDS and not self.money_known:
                code = "non_rub_money"
            elif fields & R.LEAD_FIELDS and self.goals_code:
                code = self.goals_code
            if code is None:
                continue
            self.refuse(refusal(code, USER + r.id, login=self.acc.login))
            if r.action != R.PROTECT:
                self.unverified(USER + r.id)

    def _spam(self) -> Tuple[Dict[str, str], Optional[str]]:
        """Спам-площадки логина и ошибка отчёта (или None). Правило дополняет чистку,
        а не блокирует её, но без него логин считается просмотренным не до конца."""
        acc, params = self.acc, self.settings["spam"]
        try:
            if params.get("goals") == "leads":
                if self.goals_code:
                    self.refuse(refusal(self.goals_code, "spam", login=acc.login))
                    self.unverified("spam")
                    return {}, None
                # Без цели стратегии у строки берётся самая массовая цель-лид — как
                # максимум у встроенных в этапе 1, не сумма.
                week, goals = self.report(7, self.goals), {}
            else:
                goals = direct.campaign_goals(acc.token, acc.login)
                if not goals:
                    return {}, None
                week = direct.placements_with_goals(acc.token, acc.login,
                                                    sorted(set(goals.values())))
            return spam.suspicious_sites(week, goals, min_leads=params["min_leads"],
                                         min_cr=params["min_cr"], ratio=params["ratio"]), None
        except Exception as err:
            self.log("  спам-правило пропущено: %s" % err)
            return {}, "спам-правило: %s" % err

    def _plan(self, rows, campaigns, ask, use_db: bool) -> dict:
        s = self.settings
        forced = [_site(x) for x in s["always_block"] if _site(x)]
        suspicious: Dict[str, str] = {}
        if s["spam_rule"]:
            suspicious, bad = self._spam()
            if bad:
                self.out.problems.append(bad)
        kwargs: Dict[str, Any] = {
            "top_n": s["top_n"], "always_block": forced, "spam": suspicious,
            "cut_verdicts": frozenset(s["dictionary"]["categories"]) | PINNED_VERDICTS,
            "guard": lambda site, cid, verdict: self.guard(site, int(cid), verdict, False)}
        if self.extra:
            kwargs["allow_exact"] = set(classify.ALLOW_EXACT) | self.extra
        plan = planner.plan_account(rows, campaigns, **kwargs)
        if ask is not None:
            overrides, note, failed = ask_model(plan, ask, use_db, self.log)
            if note:
                self.log(note)
            if failed:
                self.out.problems.append("модель не ответила")
            if overrides:
                plan = planner.plan_account(rows, campaigns, overrides=overrides, **kwargs)
        return plan

    def _builtin_params(self, verdict: str) -> dict:
        s = self.settings
        if verdict in DICTIONARY:
            return {"categories": list(s["dictionary"]["categories"]), "top_n": s["top_n"]}
        if verdict == "spam":
            return dict(s["spam"])
        return {}

    def _builtin_findings(self, plan: dict, live: List[dict]) -> None:
        login, out = self.acc.login, self.out
        out.refused += [_refusal_line(login, r) for r in plan["refused"]]
        for item in plan["guarded"]:
            self.guarded(item)
        added: Dict[int, int] = {}
        for action in plan["actions"]:
            cid = int(action["campaign_id"])
            limit = direct.sites_limit((self.by_id.get(cid) or {}).get("Type"))
            for a in action["added"]:
                site, verdict = a["placement"], a["verdict"]
                name = BUILTIN_NAMES.get(verdict, verdict)
                f = Finding(
                    rule=verdict, severity="info",
                    title="Запретить площадку %s в кампании %s" % (site, cid),
                    login=login, campaign_id=cid,
                    campaign_name=action.get("campaign_name") or "",
                    object_type="site", object_id=site,
                    details={"clicks": a["clicks"], "cost": a["cost"],
                             "reason": a["reason"], "fill_after": action["fill_after"],
                             "rule": {"id": verdict, "name": name, "builtin": True,
                                      "params": self._builtin_params(verdict)},
                             "matched": [{"id": verdict, "name": name, "conds": []}],
                             "window": _window(self.now, 0),
                             "metrics": _metrics(self.frames[0].seen.get((cid, site))),
                             "list_fill": {"before": action["existing_count"], "limit": limit}},
                    action={"op": "exclude_site", "campaign_id": cid, "site": site})
                out.findings.append(f)
                self.builtin[(cid, site)] = f
                added[cid] = added.get(cid, 0) + 1
        # Слоты, оставшиеся правилам пользователя: тот же потолок 0,9 от лимита, что у
        # встроенных и у записи, минус то, что встроенные заняли в этом прогоне.
        for c in live:
            cid = int(c["Id"])
            ceiling = planner.ceiling_for(direct.sites_limit(c.get("Type")))
            used = len(self.existing(c)) + added.get(cid, 0)
            out.room[(login, cid)] = max(ceiling - used, 0)

    def _user(self, live: List[dict]) -> None:
        login = self.acc.login
        live_ids = sorted(int(c["Id"]) for c in live)
        # Кампании этого логина: из API и из строк отчётов (Мастер кампаний API не
        # отдаёт). only — список на весь проект, чужие кампании в нём не наши.
        owned = set(self.by_id).union(*(f.cost_by_campaign for f in self.frames.values()))
        results: Dict[Tuple[int, str], dict] = {}
        for rule in self.rules:
            if rule.action == R.PROTECT or rule.window not in self.frames:
                continue
            key = USER + rule.id
            frame = self.frames[rule.window]
            rows = frame.account.values() if rule.where == "account" else frame.seen.values()
            for row in rows:
                m = R.evaluate([rule], row)
                if m is None:
                    continue
                if m.rule is None:
                    code, whole = self.unknown(rule, row)
                    cid = None if whole else row.campaign_id
                    self.unverified(key, cid)
                    extra = ({"cost": frame.spend(cid)} if code == "goal_not_visible"
                             else {})
                    self.refuse(refusal(code, key, login=login, campaign_id=cid, **extra))
                    continue
                hit = {"id": key, "name": rule.name, "conds": m.matched[0]["conds"]}
                if rule.where == "account":
                    # Кампания, названная в only, но остановленная или не того типа, идёт
                    # дальше к проверке пригодности: человек должен увидеть отказ, а не
                    # тишину, — как у where:"seen".
                    pool = sorted(rule.only & owned) if rule.only else live_ids
                    targets = [cid for cid in pool if cid not in rule.except_]
                else:
                    targets = [row.campaign_id]
                for cid in targets:
                    entry = results.setdefault((cid, row.object), {"first": None, "matched": []})
                    entry["matched"].append(hit)
                    # Правила идут в порядке списка: первое совпавшее и действует.
                    if entry["first"] is None:
                        entry["first"] = (rule, row)

        for (cid, site), entry in sorted(results.items()):
            builtin = self.builtin.get((cid, site))
            if builtin is not None:
                # Встроенное раньше по порядку: действие одно, совпавшие — «сработали также».
                builtin.details["matched"].extend(entry["matched"])
                continue
            rule, row = entry["first"]
            key = USER + rule.id
            campaign = self.by_id.get(cid)
            if campaign is None or campaign.get("Type") not in direct.CLEANABLE_TYPES:
                self.refuse(refusal("campaign_type_unsupported", key, login=login,
                                    campaign_id=cid))
                continue
            if campaign.get("State") not in direct.CLEANABLE_STATES:
                self.refuse(refusal("campaign_stopped", key, login=login, campaign_id=cid))
                continue
            existing = self.existing(campaign)
            if site in existing:
                continue
            ok, why = planner.site_is_valid(site)
            if not ok:
                self.log("  [%s] %s в кампании %s не пишется: %s" % (login, site, cid, why))
                continue
            held = self.guard(site, cid, key, True)
            if held is not None:
                self.guarded(held)
                continue
            self.out.candidates.append(_Candidate(
                login=login, campaign=campaign, site=site, rule=rule, row=row,
                matched=entry["matched"], list_before=len(existing),
                list_limit=direct.sites_limit(campaign.get("Type"))))


def _user_finding(c: _Candidate, now: datetime) -> Finding:
    key = USER + c.rule.id
    snap = dict(R.snapshot(c.rule), id=key)
    details = {"clicks": int(c.row.metrics.get("clicks") or 0), "cost": leads.r2(c.cost),
               "rule": snap, "matched": c.matched, "window": _window(now, c.rule.window),
               "metrics": _metrics(c.row),
               "list_fill": {"before": c.list_before, "limit": c.list_limit}}
    if c.rule.action == "propose":
        # Авторежим такие не одобряет (store.approve_auto): решение за человеком.
        details["propose"] = True
    return Finding(rule=key, severity="info",
                   title="Запретить площадку %s в кампании %s" % (c.site, c.campaign_id),
                   login=c.login, campaign_id=c.campaign_id,
                   campaign_name=c.campaign.get("Name") or "",
                   object_type="site", object_id=c.site, details=details,
                   action={"op": "exclude_site", "campaign_id": c.campaign_id,
                           "site": c.site})


def _select(candidates: List[_Candidate], room: Dict[Tuple[str, int], int], cap: int,
            refused: List[Any], now: datetime) -> Tuple[List[Finding], Dict[str, int]]:
    """Совпадения правил пользователя -> находки в пределах max_per_run и слотов списка.

    Лимит общий на прогон, поэтому выбор идёт после всех логинов: внутри лимита
    первыми дорогие строки (расход за окно правила). Не влезшие в список кампании —
    list_full и лимит не тратят; сверх лимита — over_cap по правилу.
    """
    findings: List[Finding] = []
    over: Dict[str, int] = {}
    full: Set[Tuple[str, int, str]] = set()
    for c in sorted(candidates, key=lambda x: (-x.cost, x.login, x.campaign_id, x.site)):
        key = USER + c.rule.id
        slot = (c.login, c.campaign_id)
        if room.get(slot, 0) <= 0:
            if (c.login, c.campaign_id, key) not in full:
                full.add((c.login, c.campaign_id, key))
                refused.append(refusal("list_full", key, login=c.login,
                                       campaign_id=c.campaign_id))
            continue
        if len(findings) >= cap:
            over[key] = over.get(key, 0) + 1
            continue
        room[slot] -= 1
        findings.append(_user_finding(c, now))
    return findings, over


def _silent_keys(settings: dict, rules: List[R.Rule]) -> List[str]:
    """Правила, которые прогон намеренно не проверял: выключенные категории словаря,
    выключенный спам, выключенные и невалидные правила. Их живые находки reconcile
    не закрывает: выключить правило — не значит признать его находки исправленными."""
    keys = [c for c in DICTIONARY if c not in settings["dictionary"]["categories"]]
    if not settings["spam_rule"]:
        keys.append("spam")
    active = {r.id for r in rules}
    for item in settings.get("rules") or []:
        rid = item.get("id") if isinstance(item, dict) else None
        if isinstance(rid, str) and rid and rid not in active and USER + rid not in keys:
            keys.append(USER + rid)
    return keys


def run(ctx: Context) -> RunOutput:
    use_db = bool(os.environ.get("DATABASE_URL"))
    ask = llm.asker() if os.environ.get(LLM_ENV) == "1" else None
    if ask is not None and use_db:
        try:
            journal.ensure_tables()
        except Exception as err:
            ctx.log("кэш вердиктов недоступен (%s) — прогон идёт без него" % err)
            use_db = False

    settings = ctx.settings
    rules, notes = R.compile_rules(KEY, settings.get("rules") or [])
    goals, goals_code = leads.lead_goals(settings, ctx.project_goals)
    silent = _silent_keys(settings, rules)

    findings: List[Finding] = []
    candidates: List[_Candidate] = []
    refused: List[Any] = [refusal("rule_invalid", USER + n["rule_id"] if n.get("rule_id") else None,
                                  detail=n.get("detail")) for n in notes]
    unverified: List[tuple] = []
    room: Dict[Tuple[str, int], int] = {}
    read = {"logins": 0, "campaigns": 0, "objects": 0}
    scanned: List[str] = []
    errors: List[str] = []
    total = len(ctx.accounts)
    for i, acc in enumerate(ctx.accounts, 1):
        try:
            scan = _Login(acc, settings, rules, goals, goals_code, ctx.now, ctx.log).scan(ask, use_db)
        except Exception as err:  # кабинет не должен ронять остальные
            errors.append("%s: %s" % (acc.login, err))
            ctx.log("[%s] ошибка: %s" % (acc.login, err))
        else:
            read["logins"] += 1
            read["campaigns"] += scan.campaigns
            read["objects"] += scan.objects
            findings += scan.findings
            candidates += scan.candidates
            refused += scan.refused
            room.update(scan.room)
            unverified += scan.unverified + [(acc.login, k) for k in silent]
            if scan.problems:
                # Улики неполные: не закрываем и не гасим его находки по устаревшим данным.
                errors += ["%s: %s" % (acc.login, p) for p in scan.problems]
            else:
                scanned.append(acc.login)
        ctx.progress({"step": acc.login, "logins_done": i, "logins_total": total})
    if errors and not read["logins"]:
        raise RuntimeError("ни один логин не прочитан: " + "; ".join(errors))

    chosen, over = _select(candidates, room, settings["max_per_run"], refused, ctx.now)
    findings += chosen
    summary: Dict[str, Any] = {"refused": refused, "read": read,
                               "by_rule": by_rule([USER + r.id for r in rules], findings, refused, over),
                               # Число, как в queries: по правилам счёт — в by_rule.
                               "over_cap": sum(over.values())}
    if silent:
        # Те же ключи лежат в unverified каждого логина: по этому списку UI отличает
        # «выключено настройкой» от «не проверено» (спам выключен по умолчанию).
        summary["disabled"] = silent
    if errors:
        summary["errors"] = errors
    # Упавший логин не сканировали: его открытые находки не закрываем.
    return RunOutput(findings=findings, summary=summary, scanned_logins=scanned,
                     unverified=unverified)


def _fresh(acc: DirectAccount, campaign_id: int):
    """Тип, состояние и список запретов кампании свежим чтением; None — API её не видит."""
    rows = direct_api.get_all(acc.token, acc.login, "campaigns", {"Ids": [int(campaign_id)]},
                              ["Id", "Type", "State", FIELD])
    if not rows:
        return None
    row = rows[0]
    return row.get("Type"), row.get("State"), list((row.get(FIELD) or {}).get("Items") or [])


def _write(acc: DirectAccount, campaign_id: int, items: List[str]) -> None:
    """Записать список; отказ Директа поднимается ошибкой с его кодом и текстом."""
    # Пустой список уходит как null: ExcludedSites в спецификации Директа v5
    # (campaigns/update) nillable, а Items в нём обязателен, так что {"Items": []}
    # мог бы быть отклонён.
    res = direct_api.update_campaign_list(acc.token, acc.login, campaign_id, FIELD,
                                          items or None)
    errors = (res or {}).get("Errors") or []
    if errors:
        raise direct.DirectError("; ".join(
            "%s %s" % (e.get("Code"), e.get("Message")) for e in errors))


def _write_and_read(acc: DirectAccount, cid: int, items: List[str]):
    """Запись и чтение обратно: (что в списке сейчас, ошибка записи или None).

    Таймаут мог прийти уже после того, как Директ запись принял, поэтому чтение
    идёт при любом исходе записи, и решает оно. Не прочиталось и оно: исход
    неизвестен, ошибка уходит наверх."""
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


def _groups(items: List[StoredFinding]) -> Dict[Tuple[str, int], List[StoredFinding]]:
    out: Dict[Tuple[str, int], List[StoredFinding]] = {}
    for f in items:
        out.setdefault((f.login, int(f.action["campaign_id"])), []).append(f)
    return out


def _fail_all(group, error) -> Dict[int, ApplyResult]:
    return {f.id: ApplyResult(False, {}, error) for f in group}


NOT_FOUND_BACK = "кампания не найдена при чтении обратно"


def _apply_group(acc, cid, group) -> Dict[int, ApplyResult]:
    fresh = _fresh(acc, cid)
    if fresh is None:
        return _fail_all(group, "кампания не видна API (Мастер кампаний)")
    ctype, state, existing = fresh
    if state not in direct.CLEANABLE_STATES:
        return _fail_all(group, "кампания не запущена (состояние %s)" % state)
    have = {_site(s) for s in existing}
    limit = direct.sites_limit(ctype)
    ceiling = planner.ceiling_for(limit)
    room = max(ceiling - len(existing), 0)
    out: Dict[int, ApplyResult] = {}
    to_add: List[str] = []
    for f in group:
        site = _site(f.action["site"])
        if site in have:
            # Это не наш запрет: помечать applied нельзя, иначе откат снимет чужое.
            out[f.id] = ApplyResult(False, {"already_present": True, "site": site},
                                    "площадка уже запрещена в кампании — запрет не робота")
        elif site in to_add:
            # Вторая находка на ту же площадку: запись одна, засчитать её дважды нельзя
            # (откат первой снял бы запрет, который «держит» и вторая).
            out[f.id] = ApplyResult(False, {}, "дубль: та же площадка в этом применении")
        elif len(to_add) >= room:
            out[f.id] = ApplyResult(False, {}, "список запретов заполнен (%d из %d)"
                                    % (len(existing), limit))
        else:
            to_add.append(site)
    if not to_add:
        return out
    # Прежние элементы остаются как есть и первыми: в списке есть и ручные запреты.
    back, werr = _write_and_read(acc, cid, existing + to_add)
    if back is None:
        out.update({f.id: ApplyResult(False, {}, NOT_FOUND_BACK)
                    for f in group if f.id not in out})
        return out
    present = {_site(s) for s in back}
    for f in group:
        if f.id in out:
            continue
        site = _site(f.action["site"])
        if site in present:
            out[f.id] = ApplyResult(True, {"campaign_id": cid, "site": site,
                                           "fill_after": len(back)})
        elif werr is not None:
            out[f.id] = ApplyResult(False, {"campaign_id": cid, "site": site},
                                    "%s: %s" % (type(werr).__name__, werr))
        else:
            out[f.id] = ApplyResult(False, {"campaign_id": cid, "site": site},
                                    "Директ принял, но не применил")
    return out


def _rollback_group(acc, cid, group) -> Dict[int, ApplyResult]:
    fresh = _fresh(acc, cid)
    if fresh is None:
        return _fail_all(group, "кампания не видна API (Мастер кампаний)")
    existing = fresh[2]
    out: Dict[int, ApplyResult] = {}
    drop = set()
    for f in group:
        site = f.action["site"]
        if any(s.strip() == site for s in existing):
            drop.add(site)
        elif any(_site(s) == _site(site) for s in existing):
            # Робот писал нормализованную форму; вариант в другом регистре поставил человек.
            out[f.id] = ApplyResult(False, {"campaign_id": cid, "site": site},
                                    "в списке вариант в другом регистре — запрет не робота")
        else:
            out[f.id] = ApplyResult(True, {"campaign_id": cid, "site": site,
                                           "already_absent": True})
    if not drop:
        return out
    keep = [s for s in existing if s.strip() not in drop]
    back, werr = _write_and_read(acc, cid, keep)
    if back is None:
        out.update({f.id: ApplyResult(False, {}, NOT_FOUND_BACK)
                    for f in group if f.id not in out})
        return out
    present = {s.strip() for s in back}
    for f in group:
        if f.id in out:
            continue
        site = f.action["site"]
        if site not in present:
            out[f.id] = ApplyResult(True, {"campaign_id": cid, "site": site})
        elif werr is not None:
            out[f.id] = ApplyResult(False, {"campaign_id": cid, "site": site},
                                    "%s: %s" % (type(werr).__name__, werr))
        else:
            out[f.id] = ApplyResult(False, {"campaign_id": cid, "site": site},
                                    "площадка осталась в списке после снятия")
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
            out.update(handler(acc, cid, group))
        except Exception as err:  # одна кампания не должна ронять остальные
            out.update(_fail_all(group, "%s: %s" % (type(err).__name__, err)))
    return out


def apply(ctx: Context, items: List[StoredFinding]) -> Dict[int, ApplyResult]:
    return _run_groups(ctx, items, _apply_group)


def rollback(ctx: Context, items: List[StoredFinding]) -> Dict[int, ApplyResult]:
    return _run_groups(ctx, items, _rollback_group)
