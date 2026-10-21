# -*- coding: utf-8 -*-
"""Раннер модулей Директа: замок проекта -> применение одобренного -> due-модули -> сверка.

Пробные прогоны (module_previews) берутся в начале такта, между модулями и в конце, а при
занятом замке — до выхода: превью только читает, замок ему не нужен.
"""

import dataclasses
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from sync.modules import accounts as _accounts
from sync.modules import leads as _leads
from sync.modules import store as _store
from sync.modules.defaults import settings_for
from sync.modules.lifecycle import reconcile
from sync.modules.placements import USER as USER_RULE
from sync.modules.registry import MODULES
from sync.modules.types import ApplyResult, Context, Finding, RunOutput, StoredFinding

# Москва без перехода на летнее время с 2014: фиксированное смещение не требует базы tz.
MSK = timezone(timedelta(hours=3))
DAILY_FROM_HOUR = 7
RETRY_AFTER = timedelta(hours=2)
# Сутки закрывает только прогон, дошедший до конца; error день не закрывает.
DONE_STATUSES = ("ok", "skipped")
WRITE_MODES = ("approve", "auto")
# Откат — снятие нашей же записи по просьбе человека: выполняется и в report, но не в off.
ROLLBACK_MODES = ("report", "approve", "auto")
# Строка module_runs фазы применения: её не считают сканом ни расписание, ни ручной запуск.
APPLY_PHASE = "apply"
# Юнит <slug>_modules ограничен 40 минутами: TimeoutStartSec из timeout="40min" в
# infra/panda/sync/gen-units.py (репо Panda-BI). Строка «идёт» вдвое дольше — прогон убит
# systemd и уже не завершится.
STALE_RUN_AFTER = timedelta(minutes=80)
EDU_LOCK_LINE = "агент EDU включён — запись у него"
# Пределы списков в jsonb (спека «Пробный прогон»): рядом всегда полный счёт, по нему
# Panda пишет «Показаны первые N из M».
PREVIEW_WOULD_MAX = 500
PREVIEW_REFUSED_MAX = 300
SUMMARY_REFUSED_MAX = 300
RUB = "RUB"
# Юнит живёт 40 минут (см. STALE_RUN_AFTER). Превью до применения и между модулями берём,
# только пока такт моложе половины: остальное время — применению одобренного и сканам,
# иначе systemd убьёт процесс посреди скана. Конец такта бюджета не имеет: работы после
# него нет, а заявка иначе ждала бы следующего такта.
PREVIEW_TICK_BUDGET = timedelta(minutes=20)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_due(module_key: str, last_ok: Optional[datetime], last_attempt: Optional[datetime],
           run_requested_at: Optional[datetime], now: datetime) -> bool:
    """last_ok — старт последнего прогона ok/skipped, last_attempt — последнего любого."""
    now = _aware(now)
    if run_requested_at is not None and (
            last_attempt is None or _aware(run_requested_at) > _aware(last_attempt)):
        return True
    if module_key == "placements":
        return True
    now_msk = now.astimezone(MSK)
    if now_msk.hour < DAILY_FROM_HOUR:
        return False
    if last_ok is not None and _aware(last_ok).astimezone(MSK).date() >= now_msk.date():
        return False
    # упавший прогон дня не закрывает, но и долбить Директ каждые полчаса незачем
    if last_attempt is not None and now - _aware(last_attempt) < RETRY_AFTER:
        return False
    return True


def _live_autonomy() -> str:
    # Живая автономия лежит в БД: resolve() сам по себе вернул бы только кодовые умолчания.
    from sync.agent import config as agent_config
    from sync.agent import db as agent_db
    cfg = agent_db.load_agent_config()
    return agent_config.resolve(cfg["preset"], cfg["overrides"])["autonomy"]


def agent_lock(slug: str, read_autonomy: Callable[[], str] = _live_autonomy) -> Optional[str]:
    """Агент EDU пишет в те же кампании: пока он включён, модули только наблюдают."""
    if slug != "edunetwork":
        return None
    try:
        return None if read_autonomy() == "off" else EDU_LOCK_LINE
    except Exception:
        # конфиг не прочитался: считаем агента включённым, двойная запись хуже пропуска
        return EDU_LOCK_LINE


def _ctx(slug, mkey, mode, raw_settings, accts, now, log, goals: Iterable[int] = (),
         progress: Optional[Callable[[dict], None]] = None) -> Context:
    extra = {"progress": progress} if progress is not None else {}
    return Context(slug=slug, module_key=mkey, mode=mode,
                   settings=settings_for(mkey, raw_settings),
                   accounts=accts, now=now, log=log, project_goals=list(goals), **extra)


def _currencies(accts) -> Dict[str, str]:
    # как money_known модулей: валюта не указана — рубли
    return {a.login: (a.currency or RUB).upper() for a in accts}


def _unverified_by_login(items: Iterable[tuple], skip: Iterable[str] = ()) -> Dict[str, list]:
    """Форма summary.unverified: {логин: [правило | [правило, кампания]]} — пара строкой,
    тройка списком из двух; так её читает Panda (unverifiedOf)."""
    off = set(skip)
    out: Dict[str, list] = {}
    for u in items:
        if u[1] in off:
            continue
        out.setdefault(u[0], []).append(u[1] if len(u) == 2 else [u[1], u[2]])
    return out


def _safe_call(fn, ctx, items, log, what):
    """Результат модуля по id и текст сбоя (None — сбоя не было); сбой или не-словарь
    превращаем в отказ для каждой находки."""
    try:
        res = fn(ctx, items)
        if isinstance(res, dict):
            return res, None
        err = "модуль вернул %s вместо словаря результатов" % type(res).__name__
    except Exception as exc:
        err = "%s: %s" % (type(exc).__name__, exc)
    log("[%s] ошибка: %s" % (what, err))
    return {f.id: ApplyResult(False, {}, err) for f in items}, err


def _record(store, f, res, rollback, st, log) -> None:
    if rollback:
        status, key = ("rolled_back", "rolled_back") if res.ok else ("applied", "rollback_refused")
    else:
        status, key = ("applied", "applied") if res.ok else ("failed", "failed")
    if store.set_outcome(f.id, status, res, rollback=rollback):
        st[key] = st.get(key, 0) + 1
    else:
        # человек сменил статус, пока шёл запрос: его решение главнее
        st["skipped_changed"] = st.get("skipped_changed", 0) + 1
        log("находка %s: статус изменился во время применения" % f.id)


def _wanted(f: StoredFinding, mode: str) -> bool:
    """Откат — явная отмена человека и всегда в безопасную сторону (снимает нашу же запись),
    поэтому идёт и в report; одобренное пишется в Директ только в approve/auto."""
    if f.status == "rollback_requested":
        return mode in ROLLBACK_MODES
    return mode in WRITE_MODES


def _apply_phase(slug, store, rows, modules, accts, now, log, lock_line) -> bool:
    """Одобренное применяем, запрошенный откат снимаем; True — были сбои.

    Каждый модуль, у которого была работа, получает свою строку module_runs с
    summary.phase = "apply": queries и audit сканируются раз в сутки, и без неё итог
    применения не попал бы ни в один прогон.
    """
    failed = False
    # rows — только включённые модули: в off находки остаются как есть
    by_key = {r["module_key"]: r for r in rows}
    pend: Dict[str, List[StoredFinding]] = {}
    for f in store.pending(slug):
        mkey = f.fingerprint.split(":", 1)[0]
        row = by_key.get(mkey)
        if row is not None and _wanted(f, row["mode"]):
            pend.setdefault(mkey, []).append(f)
    if pend and lock_line:
        log("запись заблокирована: %s" % lock_line)
        return failed
    for mkey, items in pend.items():
        module = modules.get(mkey)
        if module is None or not accts:
            log("[%s] ждёт применения %d находок, но %s" % (
                mkey, len(items), "модуль не реализован" if module is None else "нет кабинетов"))
            continue
        parts = []
        for rollback in (False, True):
            part = [f for f in items if (f.status == "rollback_requested") == rollback]
            fn = getattr(module, "rollback" if rollback else "apply", None)
            if part and fn is not None:
                parts.append((rollback, fn, part))
        if not parts:
            continue
        row = by_key[mkey]
        ctx = _ctx(slug, mkey, row["mode"], row.get("settings"), accts, now, log)
        run_id = store.start_run(slug, mkey, row["mode"], phase=APPLY_PHASE)
        st: dict = {}
        errors: List[str] = []
        try:
            for rollback, fn, part in parts:
                results, err = _safe_call(fn, ctx, part, log,
                                          "%s.%s" % (mkey, "rollback" if rollback else "apply"))
                if err is not None:
                    # не "errors": это имя у модулей занято списком строк по кабинетам
                    st["apply_errors"] = st.get("apply_errors", 0) + 1
                    errors.append(err)
                for f in part:
                    res = results.get(f.id) or ApplyResult(False, {}, "модуль не вернул результат")
                    _record(store, f, res, rollback, st, log)
        except Exception as exc:
            errors.append("%s: %s" % (type(exc).__name__, exc))
            store.finish_run(run_id, "error", {"phase": APPLY_PHASE, "apply": st},
                             error="; ".join(errors))
            raise
        log("[%s] применение: %s" % (mkey, st))
        store.finish_run(run_id, "error" if errors else "ok", {"phase": APPLY_PHASE, "apply": st},
                         error="; ".join(errors) or None)
        failed = failed or bool(errors)
    return failed


def _auto_apply(slug, mkey, module, ctx, store, fresh, confirmed, log):
    """Только подтверждённое этим сканом: вставленное сейчас и открытое, увиденное снова.

    Открытая находка без подтверждения живёт до 72 ч; банить по устаревшим данным нельзя.
    """
    out: dict = {}
    if not hasattr(module, "apply"):
        return out, False
    cand = [f.id for f in fresh if f.id in confirmed and f.status == "open"
            and f.action is not None]
    # approve_auto не одобряет то, что человек уже откатывал: оно ждёт его решения
    ids = set(store.approve_auto(slug, mkey, cand)) if cand else set()
    if len(ids) < len(cand):
        out["held"] = len(cand) - len(ids)
    items = [dataclasses.replace(f, status="approved") for f in fresh if f.id in ids]
    if not items:
        return out, False
    results, err = _safe_call(module.apply, ctx, items, log, "%s.apply" % mkey)
    for f in items:
        res = results.get(f.id) or ApplyResult(False, {}, "модуль не вернул результат")
        _record(store, f, res, False, out, log)
    return out, err is not None


def _dry_run(slug, store, accts, modules, now, only, log) -> int:
    """Чтение базы можно (настройки проекта), запись и замок — нет."""
    try:
        rows = store.load_project_modules(slug, include_off=True)
        if only:
            # явно названный модуль гоняем, даже если в проекте он выключен, — со своими
            # сохранёнными настройками, а не умолчаниями
            by_key = {r["module_key"]: r for r in rows}
            rows = [by_key.get(only) or {"module_key": only, "settings": None}]
        else:
            rows = [r for r in rows if r["mode"] != "off"]
    except Exception as err:
        log("предупреждение: настройки проекта не прочитаны (%s: %s), беру умолчания"
            % (type(err).__name__, err))
        rows = [{"module_key": k, "settings": None} for k in modules if not only or k == only]
    try:
        goals = store.project_lead_goals(slug)
    except Exception as err:
        log("предупреждение: цели-лиды проекта не прочитаны (%s: %s)" % (type(err).__name__, err))
        goals = []
    rc = 0
    for row in rows:
        mkey = row["module_key"]
        module = modules.get(mkey)
        if module is None:
            log("[%s] модуль не реализован" % mkey)
            continue
        try:
            out = module.run(_ctx(slug, mkey, "report", row.get("settings"), accts, now, log,
                                  goals))
        except Exception as err:
            log("[%s] ошибка: %s: %s" % (mkey, type(err).__name__, err))
            rc = 1
            continue
        log("[%s] находок: %d" % (mkey, len(out.findings)))
        for f in out.findings:
            log("  %s | %s | %s | %s | %s" % (f.severity, f.login, f.object_id, f.rule, f.title))
    return rc


def _held_first(items: list) -> list:
    """Удержания защитой (held) — наверх, остальной порядок модуля как был. Panda громко
    пишет «защита не проверяется» только по элементам с held: обрезка до 300 не должна
    прятать их за сотнями обычных отказов большого проекта."""
    return sorted(items, key=lambda x: not (isinstance(x, dict) and x.get("held")))


def _by_code(items: Iterable[Any]) -> Dict[str, int]:
    """Полный счёт отказов по коду: группы в UI не должны заниживаться обрезкой списка."""
    out: Dict[str, int] = {}
    for x in items:
        code = x.get("code") if isinstance(x, dict) else None
        if code:
            out[code] = out.get(code, 0) + 1
    return out


def _scan_summary(out: RunOutput, accts, cur: Dict[str, str]) -> dict:
    """summary строки module_runs из итога модуля: unverified по логинам, отказы с пределом."""
    summary = dict(out.summary)
    if out.unverified:
        summary["unverified"] = _unverified_by_login(out.unverified)
    refused = summary.get("refused")
    if isinstance(refused, list):
        # by_rule модуль посчитал по полному списку; урезаем только то, что ляжет в jsonb
        summary["refused_total"] = len(refused)
        summary["refused_by_code"] = _by_code(refused)
        summary["refused"] = _held_first(refused)[:SUMMARY_REFUSED_MAX]
    # «кабинет N из M»: из errors M не вывести — проблемы прочитанного логина тоже там
    summary["logins_total"] = len(accts)
    other = {login: c for login, c in cur.items() if c != RUB}
    if other:
        summary["currency_by_login"] = other
    return summary


class _NotRun(Exception):
    """Превью не начато по понятной причине: текст идёт человеку без имени исключения."""


def _num(v) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def _would_row(f: Finding, cur: str) -> dict:
    d = f.details or {}
    m = d.get("metrics")
    # находки этапа 1 без снимка metrics несут клики и расход прямо в details
    metrics = m if isinstance(m, dict) else {
        k: d[k] for k in ("clicks", "cost", "conversions") if k in d}
    row = {"object": f.object_id, "campaign_id": f.campaign_id,
           "campaign_name": f.campaign_name or None, "login": f.login, "rule": f.rule,
           "currency": cur, "metrics": metrics, "action": f.action}
    # Panda (familyTail, «шире, чем запрос») читает эти поля с would, а не из metrics:
    # без них превью против настоящего раннера не показывает семью запросов
    for k in ("metrics_scope", "family_queries", "broad"):
        if k in d:
            row[k] = d[k]
    return row


def _preview_result(out: RunOutput, rule_id: Optional[str], accts,
                    lead_goals: Optional[List[int]] = None) -> dict:
    """result превью по контракту Panda (adm-module-rules.ts, previewView): would — те же
    находки, что записал бы скан; тексты Panda рисует по снимку настроек сама.

    Превью одного правила — только его цифры: отказы, «не проверено», сверх лимита и
    улов, который сейчас забирают правила выше и защиты (by_rule.taken/protected).
    lead_goals — цели-лиды, по которым шёл прогон: Panda сверяет с ними «делать само»."""
    cur = _currencies(accts)
    findings = out.findings
    summary = out.summary or {}
    refused = summary.get("refused") or []
    # строки этапа 1 отдельно: Panda сопоставляет refused[i] со списком отказов с кодом
    coded = [x for x in refused if isinstance(x, dict)]
    lines = [x for x in refused if isinstance(x, str)]
    by_rule = {k: v for k, v in (summary.get("by_rule") or {}).items() if isinstance(v, dict)}
    caught = summary.get("intercepted") or {}
    unverified = list(out.unverified)
    key = None
    if rule_id:
        key = rule_id if rule_id.startswith(USER_RULE) else USER_RULE + rule_id
        findings = [f for f in findings if f.rule == key]
        coded = [x for x in coded if x.get("rule") == key]
        # отказы-строки — встроенных, у правила пользователя их нет
        lines = []
        by_rule = {key: by_rule.get(key) or {}}
        unverified = [u for u in unverified if u[1] == key]
    rows = [_would_row(f, cur.get(f.login, RUB)) for f in findings]
    # дорогие первыми: усечение до 500 должно резать дешёвый хвост. Курса в раннере нет,
    # тенге численно в разы больше рублей — поэтому сначала рубли, затем прочие валюты
    rows.sort(key=lambda r: (r["currency"] != RUB, -_num(r["metrics"].get("cost"))))
    cost_other: Dict[str, float] = {}
    rub = 0.0
    for r in rows:
        c = _num(r["metrics"].get("cost"))
        if r["currency"] == RUB:
            rub += c
        else:
            cost_other[r["currency"]] = cost_other.get(r["currency"], 0.0) + c
    rules_out: Dict[str, Dict[str, int]] = {}
    for k, v in by_rule.items():
        got = caught.get(k) or {}
        rules_out[k] = {"would": v.get("found", 0), "refused": v.get("refused", 0),
                        "over_cap": v.get("over_cap", 0),
                        "taken": int(got.get("taken") or 0),
                        "protected": int(got.get("protected") or 0)}
    totals: Dict[str, Any] = {"would": len(rows),
                              "clicks": int(sum(_num(r["metrics"].get("clicks")) for r in rows)),
                              "cost": round(rub, 2), "refused": len(coded)}
    if key is not None:
        mine = rules_out[key]
        # полный улов: что закрыло бы сейчас, что не влезло в лимит и что забрали другие
        totals.update(over_cap=mine["over_cap"], taken=mine["taken"],
                      protected=mine["protected"],
                      caught=mine["would"] + mine["over_cap"] + mine["taken"]
                      + mine["protected"])
    if cost_other:
        totals["cost_other"] = {k: round(v, 2) for k, v in sorted(cost_other.items())}
    disabled = list(summary.get("disabled") or [])
    result: Dict[str, Any] = {
        "would": rows[:PREVIEW_WOULD_MAX],
        "refused": _held_first(coded)[:PREVIEW_REFUSED_MAX],
        "refused_by_code": _by_code(coded),
        "by_rule": rules_out,
        # выключенное настройкой не «не проверено»: оно в disabled, иначе превью пугало бы
        # строками про спам, выключенный по умолчанию
        "unverified": _unverified_by_login(unverified, skip=disabled),
        "skipped": list(summary.get("skipped") or []),
        # та же форма «логин: текст», что summary.errors скана (Panda: errorLogins)
        "errors": [e for e in summary.get("errors") or [] if isinstance(e, str)],
        "totals": totals,
        "logins_total": len(accts),
    }
    if lead_goals is not None:
        result["lead_goals"] = list(lead_goals)
    if disabled:
        result["disabled"] = disabled
    # без валюты логина Panda рисует «₽» у отказа в тенговом кабинете
    other_cur = {login: c for login, c in cur.items() if c != RUB}
    if other_cur:
        result["currency_by_login"] = other_cur
    if lines:
        result["refused_lines"] = lines[:PREVIEW_REFUSED_MAX]
    return result


def _memo(fn: Callable[[], Any]) -> Callable[[], Any]:
    box: list = []

    def get():
        if not box:
            box.append(fn())
        return box[0]
    return get


def _one_preview(slug, store, row, get_accounts, get_goals, modules, now, log) -> bool:
    """Один пробный прогон: тот же module.run, что у скана, но в report и без записи —
    ни apply_reconciled, ни set_outcome, ни Директа. True — done."""
    pid, mkey = row["id"], row["module_key"]

    def progress(p: dict) -> None:
        try:
            store.preview_progress(pid, p)
        except Exception as err:
            # ход — подсказка человеку; из-за него превью не роняем
            log("превью %s: ход не записан (%s: %s)" % (pid, type(err).__name__, err))

    try:
        module = modules.get(mkey)
        if module is None:
            raise _NotRun("модуль %s не реализован" % mkey)
        accts, why = get_accounts()
        if not accts:
            raise _NotRun("нет кабинетов: " + "; ".join(why or ["не найдены"]))
        ctx = _ctx(slug, mkey, "report", row.get("settings"), accts, now, log,
                   get_goals(), progress)
        result = _preview_result(module.run(ctx), row.get("rule_id"), accts,
                                 _leads.lead_goals(ctx.settings, ctx.project_goals)[0])
        status, error = "done", None
    except Exception as err:
        status, result = "error", None
        error = str(err) if isinstance(err, _NotRun) else "%s: %s" % (type(err).__name__, err)
        log("[превью %s %s] ошибка: %s" % (pid, mkey, error))
    try:
        saved = store.finish_preview(pid, status, result, error=error)
    except Exception as err:
        # итог не лёг (обрыв связи, jsonb не принял значение): без второй попытки строка
        # через 20 минут закрылась бы ложным «прерван», хотя модуль отработал
        error = "не удалось сохранить результат: %s: %s" % (type(err).__name__, err)
        log("превью %s: %s" % (pid, error))
        status = "error"
        try:
            saved = store.finish_preview(pid, "error", None, error=error)
        except Exception as err2:
            log("превью %s: итог не записан (%s: %s)" % (pid, type(err2).__name__, err2))
            return False
    if not saved:
        log("превью %s закрыто как прерванное раньше, итог не записан" % pid)
    return status == "done"


def _run_previews(slug, store, accts: Callable[[], Tuple[list, list]], modules, now,
                  log, *, cleanup: bool = True,
                  keep_going: Optional[Callable[[], bool]] = None) -> None:
    """Вся очередь превью проекта. accts — функция () -> (кабинеты, почему нет): при занятом
    замке кабинеты читаются, только когда превью взято (у EDU это запрос в Директ).
    keep_going проверяется перед каждым превью: бюджет такта (PREVIEW_TICK_BUDGET).
    Сбой базы здесь не роняет такт: превью останется running и закроется как прерванное."""
    if cleanup:
        try:
            store.cleanup_previews(slug)
        except Exception as err:
            log("превью: старые не удалены (%s: %s)" % (type(err).__name__, err))
    goals = _memo(lambda: store.project_lead_goals(slug))
    try:
        while True:
            if keep_going is not None and not keep_going():
                log("превью: бюджет такта исчерпан, очередь возьмёт конец такта")
                return
            row = store.claim_preview(slug)
            if row is None:
                return
            _one_preview(slug, store, row, accts, goals, modules, now, log)
    except Exception as err:
        log("превью: очередь прервана (%s: %s)" % (type(err).__name__, err))


def run_preview(slug: str, preview_id: int, *, store=_store, accounts=_accounts.accounts_for,
                modules=MODULES, now: datetime, log=print) -> int:
    """Ручной прогон одного превью (modules_run --preview-id), без скана и замка."""
    row = store.claim_preview(slug, preview_id=preview_id)
    if row is None:
        log("превью %s проекта %s не найдено или уже идёт" % (preview_id, slug))
        return 1
    ok = _one_preview(slug, store, row, _memo(lambda: accounts(slug)),
                      _memo(lambda: store.project_lead_goals(slug)), modules, now, log)
    return 0 if ok else 1


def run_project(slug: str, *, store=_store, accounts=_accounts.accounts_for, modules=MODULES,
                now: datetime, only: Optional[str] = None, dry_run: bool = False,
                log=print, agent_lock_fn=agent_lock,
                clock: Callable[[], float] = time.monotonic) -> int:
    """Код 1, если упал хоть один модуль; занятый замок — не ошибка."""
    if dry_run:
        accts, _why = accounts(slug)
        return _dry_run(slug, store, accts, modules, now, only, log)
    started = clock()
    with store.hold_project_lock(slug) as got:
        if not got:
            # превью только читает: идущий прогон проекта ему не помеха, а ждать его конца
            # человеку незачем. Бюджета нет: применения и сканов в этом процессе не будет
            _run_previews(slug, store, _memo(lambda: accounts(slug)), modules, now, log)
            log("проект %s: уже идёт" % slug)
            return 0
        # убитый прогон (OOM, таймаут юнита) так и остался бы «идёт» навсегда
        stale = store.fail_stale_runs(slug, STALE_RUN_AFTER)
        if stale:
            log("прерванных прогонов закрыто: %d" % stale)
        accts, why = accounts(slug)
        rows = [r for r in store.load_project_modules(slug)
                if not only or r["module_key"] == only]
        if only and not rows:
            log("модуль %s выключен" % only)
        goals = store.project_lead_goals(slug)

        cleaned = False

        def previews(budget: bool) -> None:
            nonlocal cleaned
            within = (lambda: clock() - started < PREVIEW_TICK_BUDGET.total_seconds()) \
                if budget else None
            _run_previews(slug, store, lambda: (accts, why), modules, now, log,
                          cleanup=not cleaned, keep_going=within)
            cleaned = True
        previews(budget=True)
        lock_line = agent_lock_fn(slug)
        rc = 1 if _apply_phase(slug, store, rows, modules, accts, now, log, lock_line) else 0
        scanned_any = False
        for row in rows:
            mkey, mode = row["module_key"], row["mode"]
            if not is_due(mkey, store.last_run_started(slug, mkey, DONE_STATUSES),
                          store.last_run_started(slug, mkey),
                          row.get("run_requested_at"), now):
                continue
            # заявка, поданная во время скана, не ждёт следующего такта
            if scanned_any:
                previews(budget=True)
            scanned_any = True
            run_id = store.start_run(slug, mkey, mode)
            module = modules.get(mkey)
            if module is None:
                store.finish_run(run_id, "skipped", {"reason": "модуль %s не реализован" % mkey})
                continue
            if not accts:
                store.finish_run(run_id, "skipped", {
                    "reason": "нет кабинетов: " + "; ".join(why or ["не найдены"])})
                continue
            summary: dict = {}
            try:
                ctx = _ctx(slug, mkey, mode, row.get("settings"), accts, now, log, goals)
                out = module.run(ctx)
                cur = _currencies(accts)
                for f in out.findings:
                    # журнал рисует расход находки: тенге без пометки читались бы рублями
                    if cur.get(f.login, RUB) != RUB:
                        f.details["currency"] = cur[f.login]
                # свой список модуля, иначе кабинеты ctx: туда попали только разрешённые логины
                scanned = out.scanned_logins if out.scanned_logins is not None \
                    else [a.login for a in accts]
                rec = reconcile(mkey, store.existing_findings(slug, mkey), out.findings,
                                now, scanned, unverified=out.unverified)
                inserted = store.apply_reconciled(slug, mkey, run_id, rec)
                summary = _scan_summary(out, accts, cur)
                summary.update(findings=len(out.findings), inserted=len(rec.insert),
                               resolved=len(rec.resolve), expired=len(rec.expire))
                if lock_line and mode in WRITE_MODES:
                    summary["agent_lock"] = lock_line
                elif mode == "auto":
                    auto, bad = _auto_apply(slug, mkey, module, ctx, store,
                                            store.existing_findings(slug, mkey),
                                            set(inserted) | {fid for fid, _ in rec.touch}, log)
                    summary.update({"auto_" + k: v for k, v in auto.items()})
                    if bad:
                        rc = 1
                store.finish_run(run_id, "ok", summary)
            except Exception as err:
                rc = 1
                text = "%s: %s" % (type(err).__name__, err)
                log("[%s] ошибка: %s" % (mkey, text))
                store.finish_run(run_id, "error", summary, error=text)
        # и без сканов: заявка, поданная во время применения, иначе ждала бы следующего такта
        previews(budget=False)
        return rc
