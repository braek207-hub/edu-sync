# -*- coding: utf-8 -*-
"""Модуль placements: чистка мусорных площадок РСЯ на движке модулей.

План запретов считает прежний sync.placements.plan (правила и ворота не менялись);
здесь он превращается в находки, а запись и откат идут по правилу движка:
свежее чтение -> слияние -> запись -> чтение обратно.
"""

import os
from typing import Dict, List, Tuple

from sync.modules import direct_api
from sync.modules.accounts import DirectAccount
from sync.modules.types import ApplyResult, Context, Finding, RunOutput, StoredFinding
from sync.placements import classify, direct, journal, llm, spam
from sync.placements import plan as planner

KEY = "placements"
FIELD = "ExcludedSites"
# Платный второй судья по умолчанию выключен, как и в старом CLI (там был --llm).
LLM_ENV = "PLACEMENTS_LLM"


def _site(name) -> str:
    return classify.normalize(name)


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


def spam_sites(token: str, login: str, log):
    """Спам-площадки логина по недельной статистике и ошибка отчёта (или None).
    Правило дополняет чистку, а не блокирует её, но без него логин считается
    просмотренным не до конца."""
    try:
        goals = direct.campaign_goals(token, login)
        if not goals:
            return {}, None
        week = direct.placements_with_goals(token, login, sorted(set(goals.values())))
        return spam.suspicious_sites(week, goals), None
    except Exception as err:
        log("  спам-правило пропущено: %s" % err)
        return {}, "спам-правило: %s" % err


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


def _scan_login(acc: DirectAccount, settings: dict, log, ask, use_db: bool):
    """Находки, отказы и сбои вспомогательных источников одного логина."""
    token = acc.token
    campaigns = direct.campaigns(token, acc.login)
    live = [c for c in campaigns
            if c.get("Type") in direct.CLEANABLE_TYPES
            and c.get("State") in direct.CLEANABLE_STATES]
    if not live:
        # У агентского кабинета половина клиентов пустая или выключена:
        # заказывать по ним отчёт — минуты прогона впустую.
        log("[%s] нечего чистить: ни одной запущенной кампании с рычагом" % acc.login)
        return [], [], []

    rows = direct.placements_today(token, acc.login)
    forced = [_site(s) for s in settings["always_block"] if _site(s)]
    problems: List[str] = []
    suspicious: Dict[str, str] = {}
    if settings["spam_rule"]:
        suspicious, bad = spam_sites(token, acc.login, log)
        if bad:
            problems.append(bad)
    kwargs = {}
    extra = {_site(s) for s in settings["extra_allow"] if _site(s)}
    if extra:
        kwargs["allow_exact"] = set(classify.ALLOW_EXACT) | extra
    top_n = settings["top_n"]
    plan = planner.plan_account(rows, campaigns, top_n=top_n, always_block=forced,
                                spam=suspicious, **kwargs)
    if ask is not None:
        overrides, note, failed = ask_model(plan, ask, use_db, log)
        if note:
            log(note)
        if failed:
            problems.append("модель не ответила")
        if overrides:
            plan = planner.plan_account(rows, campaigns, top_n=top_n, overrides=overrides,
                                        always_block=forced, spam=suspicious, **kwargs)

    findings: List[Finding] = []
    for action in plan["actions"]:
        cid = int(action["campaign_id"])
        for a in action["added"]:
            findings.append(Finding(
                rule=a["verdict"], severity="info",
                title="Запретить площадку %s в кампании %s" % (a["placement"], cid),
                login=acc.login, campaign_id=cid,
                campaign_name=action.get("campaign_name") or "",
                object_type="site", object_id=a["placement"],
                details={"clicks": a["clicks"], "cost": a["cost"],
                         "reason": a["reason"], "fill_after": action["fill_after"]},
                action={"op": "exclude_site", "campaign_id": cid, "site": a["placement"]}))
    return findings, [_refusal_line(acc.login, r) for r in plan["refused"]], problems


def run(ctx: Context) -> RunOutput:
    use_db = bool(os.environ.get("DATABASE_URL"))
    ask = llm.asker() if os.environ.get(LLM_ENV) == "1" else None
    if ask is not None and use_db:
        try:
            journal.ensure_tables()
        except Exception as err:
            ctx.log("кэш вердиктов недоступен (%s) — прогон идёт без него" % err)
            use_db = False

    findings: List[Finding] = []
    refused: List[str] = []
    scanned: List[str] = []
    errors: List[str] = []
    read_any = False
    for acc in ctx.accounts:
        try:
            f, r, problems = _scan_login(acc, ctx.settings, ctx.log, ask, use_db)
        except Exception as err:  # кабинет не должен ронять остальные
            errors.append("%s: %s" % (acc.login, err))
            ctx.log("[%s] ошибка: %s" % (acc.login, err))
            continue
        read_any = True
        findings += f
        refused += r
        if problems:
            # Улики неполные: не закрываем и не гасим его находки по устаревшим данным.
            errors += ["%s: %s" % (acc.login, p) for p in problems]
            continue
        scanned.append(acc.login)
    if errors and not read_any:
        raise RuntimeError("ни один логин не прочитан: " + "; ".join(errors))
    summary = {"refused": refused}
    if errors:
        summary["errors"] = errors
    # Упавший логин не сканировали: его открытые находки не закрываем.
    return RunOutput(findings=findings, summary=summary, scanned_logins=scanned)


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
    ceiling = (planner.FILL_CEILING if limit >= direct.MAX_EXCLUDED_SITES
               else int(limit * planner.FILL_SHARE))
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
            continue
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
