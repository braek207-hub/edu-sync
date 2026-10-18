# -*- coding: utf-8 -*-
"""Раннер модулей Директа: замок проекта -> применение одобренного -> due-модули -> сверка."""

import dataclasses
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from sync.modules import accounts as _accounts
from sync.modules import store as _store
from sync.modules.defaults import settings_for
from sync.modules.lifecycle import reconcile
from sync.modules.registry import MODULES
from sync.modules.types import ApplyResult, Context, StoredFinding

# Москва без перехода на летнее время с 2014: фиксированное смещение не требует базы tz.
MSK = timezone(timedelta(hours=3))
DAILY_FROM_HOUR = 7
WRITE_MODES = ("approve", "auto")
EDU_LOCK_LINE = "агент EDU включён — запись у него"


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_due(module_key: str, last_started: Optional[datetime],
           run_requested_at: Optional[datetime], now: datetime) -> bool:
    now = _aware(now)
    if run_requested_at is not None and (
            last_started is None or _aware(run_requested_at) > _aware(last_started)):
        return True
    if module_key == "placements":
        return True
    now_msk = now.astimezone(MSK)
    if now_msk.hour < DAILY_FROM_HOUR:
        return False
    if last_started is None:
        return True
    return _aware(last_started).astimezone(MSK).date() < now_msk.date()


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


def _ctx(slug, mkey, mode, raw_settings, accts, now, log) -> Context:
    return Context(slug=slug, module_key=mkey, mode=mode,
                   settings=settings_for(mkey, raw_settings),
                   accounts=accts, now=now, log=log)


def _apply_phase(slug, store, rows, modules, accts, now, log, lock_line) -> Dict[str, dict]:
    """Одобренное применяем, запрошенный откат снимаем. Вернуть счётчики по модулям."""
    stats: Dict[str, dict] = {}
    # модуль в off/report: его находки остаются как есть
    writable = {r["module_key"]: r for r in rows if r["mode"] in WRITE_MODES}
    pend: Dict[str, List[StoredFinding]] = {}
    for f in store.pending(slug):
        mkey = f.fingerprint.split(":", 1)[0]
        if mkey in writable:
            pend.setdefault(mkey, []).append(f)
    if pend and lock_line:
        log("запись заблокирована: %s" % lock_line)
        return stats
    for mkey, items in pend.items():
        module = modules.get(mkey)
        if module is None or not accts:
            continue
        row = writable[mkey]
        ctx = _ctx(slug, mkey, row["mode"], row.get("settings"), accts, now, log)
        st = stats.setdefault(mkey, {"applied": 0, "failed": 0, "rolled_back": 0,
                                     "rollback_refused": 0, "skipped_changed": 0})
        for rollback in (False, True):
            part = [f for f in items if (f.status == "rollback_requested") == rollback]
            fn = getattr(module, "rollback" if rollback else "apply", None)
            if not part or fn is None:
                continue
            results = fn(ctx, part)
            for f in part:
                res = results.get(f.id) or ApplyResult(False, {}, "модуль не вернул результат")
                if rollback:
                    status, key = ("rolled_back", "rolled_back") if res.ok \
                        else ("applied", "rollback_refused")
                else:
                    status, key = ("applied", "applied") if res.ok else ("failed", "failed")
                if store.set_outcome(f.id, status, res, rollback=rollback):
                    st[key] += 1
                else:
                    # человек сменил статус, пока шёл запрос: его решение главнее
                    st["skipped_changed"] += 1
                    log("находка %s: статус изменился во время применения" % f.id)
    return stats


def _auto_apply(slug, mkey, module, ctx, store, inserted_ids, log) -> dict:
    out = {"applied": 0, "failed": 0}
    if not hasattr(module, "apply"):
        return out
    fresh = {f.id: f for f in store.existing_findings(slug, mkey)}
    # находка без action — только для отчёта, одобрять её нечем
    cand = [i for i in inserted_ids if i in fresh and fresh[i].action is not None]
    ids = store.approve_auto(slug, mkey, cand) if cand else []
    items = [dataclasses.replace(fresh[i], status="approved") for i in ids]
    results = module.apply(ctx, items) if items else {}
    for f in items:
        res = results.get(f.id) or ApplyResult(False, {}, "модуль не вернул результат")
        if store.set_outcome(f.id, "applied" if res.ok else "failed", res):
            out["applied" if res.ok else "failed"] += 1
        else:
            log("находка %s: статус изменился во время применения" % f.id)
    return out


def _dry_run(slug, store, accts, modules, now, only, log) -> int:
    """Чтение базы можно (настройки проекта), запись и замок — нет."""
    try:
        rows = store.load_project_modules(slug)
        if only:
            # явно названный модуль гоняем, даже если в проекте он выключен
            by_key = {r["module_key"]: r for r in rows}
            rows = [by_key.get(only) or {"module_key": only, "settings": None}]
    except Exception as err:
        log("предупреждение: настройки проекта не прочитаны (%s: %s), беру умолчания"
            % (type(err).__name__, err))
        rows = [{"module_key": k, "settings": None} for k in modules if not only or k == only]
    rc = 0
    for row in rows:
        mkey = row["module_key"]
        module = modules.get(mkey)
        if module is None:
            log("[%s] модуль не реализован" % mkey)
            continue
        try:
            out = module.run(_ctx(slug, mkey, "report", row.get("settings"), accts, now, log))
        except Exception as err:
            log("[%s] ошибка: %s: %s" % (mkey, type(err).__name__, err))
            rc = 1
            continue
        log("[%s] находок: %d" % (mkey, len(out.findings)))
        for f in out.findings:
            log("  %s | %s | %s | %s | %s" % (f.severity, f.login, f.object_id, f.rule, f.title))
    return rc


def run_project(slug: str, *, store=_store, accounts=_accounts.accounts_for, modules=MODULES,
                now: datetime, only: Optional[str] = None, dry_run: bool = False,
                log=print, agent_lock_fn=agent_lock) -> int:
    """Код 1, если упал хоть один модуль; занятый замок — не ошибка."""
    accts, why = accounts(slug)
    if dry_run:
        return _dry_run(slug, store, accts, modules, now, only, log)
    with store.hold_project_lock(slug) as got:
        if not got:
            log("проект %s: замок держит другой прогон" % slug)
            return 0
        rows = [r for r in store.load_project_modules(slug)
                if not only or r["module_key"] == only]
        lock_line = agent_lock_fn(slug)
        stats = _apply_phase(slug, store, rows, modules, accts, now, log, lock_line)
        rc = 0
        for row in rows:
            mkey, mode = row["module_key"], row["mode"]
            if not is_due(mkey, store.last_run_started(slug, mkey),
                          row.get("run_requested_at"), now):
                continue
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
                ctx = _ctx(slug, mkey, mode, row.get("settings"), accts, now, log)
                out = module.run(ctx)
                # свой список модуля, иначе кабинеты ctx: туда попали только разрешённые логины
                scanned = out.scanned_logins if out.scanned_logins is not None \
                    else [a.login for a in accts]
                rec = reconcile(mkey, store.existing_findings(slug, mkey), out.findings,
                                now, scanned)
                inserted = store.apply_reconciled(slug, mkey, run_id, rec)
                summary = dict(out.summary)
                summary.update(findings=len(out.findings), inserted=len(rec.insert),
                               resolved=len(rec.resolve), expired=len(rec.expire))
                summary.update(stats.get(mkey, {}))
                if lock_line and mode in WRITE_MODES:
                    summary["agent_lock"] = lock_line
                elif mode == "auto" and inserted:
                    summary.update({"auto_" + k: v for k, v in _auto_apply(
                        slug, mkey, module, ctx, store, inserted, log).items()})
                store.finish_run(run_id, "ok", summary)
            except Exception as err:
                rc = 1
                text = "%s: %s" % (type(err).__name__, err)
                log("[%s] ошибка: %s" % (mkey, text))
                store.finish_run(run_id, "error", summary, error=text)
        return rc
