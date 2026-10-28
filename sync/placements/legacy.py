# -*- coding: utf-8 -*-
"""Прежний движок чистильщика для кабинетов вне проектов Panda-BI.

Кабинеты проектов чистит модуль placements (sync.modules). Кабинету без
проекта (accounts.legacy_accounts) модулю некуда писать находки — у него нет
строки в dashboards, — поэтому для него остаётся прежнее поведение: словарь и
клики в воротах топ-N, запись поверх свежего чтения, журнал в
placement_cleaner_cuts. Спам-правила, обязательного списка и второго судьи
здесь нет: у единственного такого кабинета (ОШ Синергия) их не было.
"""

from typing import Callable, Dict, Optional, Set, Tuple

from sync.modules import direct_api
from sync.placements import direct, journal
from sync.placements import plan as planner
from sync.placements.accounts import Account

Log = Callable[[str], None]


def set_excluded_sites(token: str, login: str, campaign_id: str,
                       sites) -> Tuple[bool, str]:
    """Записать полный список запретов кампании: (успех, сообщение).

    Ошибка одной кампании не роняет прогон — остальные вычистить всё равно надо.
    """
    try:
        item = direct_api.update_campaign_list(token, login, int(campaign_id),
                                               "ExcludedSites", list(sites) or None)
    except Exception as err:
        return False, str(err)
    errors = item.get("Errors") or []
    if errors:
        return False, "; ".join("%s %s" % (e.get("Code"), e.get("Message"))
                                for e in errors)
    return True, "; ".join(w.get("Message", "") for w in item.get("Warnings") or [])


def run_account(account: Account, apply: bool, use_db: bool, log: Log,
                top_n: int = planner.TOP_N) -> Dict[str, int]:
    token, login = account.token(), account.login or ""
    if not token or not login:
        raise RuntimeError("нет токена в %s или Client-Login" % account.token_env)
    log("\n[%s / %s] отчёт площадок за сегодня…" % (account.key, login))
    # Кампании сначала, и только свои: в общем кабинете чужие не читаются вовсе.
    campaigns = direct.campaigns(token, login, account.campaign_ids)
    live = [c for c in campaigns
            if c.get("Type") in direct.CLEANABLE_TYPES
            and c.get("State") in direct.CLEANABLE_STATES]
    if not live:
        log("  нечего чистить: ни одной запущенной кампании с рычагом")
        return {"sites": 0, "campaigns": 0, "ok": 0, "failed": 0}
    rows = direct.placements_today(token, login)
    mine = {str(x) for x in account.campaign_ids}
    if mine:
        rows = [r for r in rows if str(r.get("campaign_id") or "") in mine]
    plan = planner.plan_account(rows, campaigns, top_n=top_n)

    log("  площадок за день: %d, кликов %d, расход %.0f ₽"
        % (plan["day_sites"], plan["day_clicks"], plan["day_cost"]))
    actions = plan["actions"]
    total_sites = sum(len(a["added"]) for a in actions)
    log("  к запрету: %d площадок в %d кампаниях (%d кл, %.0f ₽)"
        % (total_sites, len(actions),
           sum(a["cut_clicks"] for a in actions),
           sum(a["cut_cost"] for a in actions)))
    for item in plan["refused"][:10]:
        money = ("  [%d кл, %.0f ₽]" % (item["clicks"], item["cost"])
                 if item.get("clicks") else "")
        log("    отказ: %s%s" % (item.get("reason"), money))

    # Журнал важен, но чистка важнее: недоступная база не повод оставить
    # кабинет грязным. Отказ журнала виден в выводе.
    run_id = None
    if use_db:
        try:
            run_id = journal.start_run(account.key, login, not apply, plan)
        except Exception as err:
            log("  ЖУРНАЛ НЕДОСТУПЕН, пишем вслепую: %s" % err)

    ok_n, bad_n = 0, 0
    for action in actions:
        head = "    %s «%s» +%d → %d слотов" % (
            action["campaign_id"], (action["campaign_name"] or "")[:38],
            len(action["added"]), action["fill_after"])
        ok = False
        if not apply:
            log(head + "  [репетиция]")
        else:
            ok, note = set_excluded_sites(token, login, action["campaign_id"],
                                          action["sites"])
            if ok:
                ok_n += 1
                log(head + ("  ✓ " + note if note else "  ✓"))
            else:
                bad_n += 1
                log(head + "  ✗ " + note)
        if run_id:
            try:
                journal.record_cuts(run_id, account.key, login, action, ok)
            except Exception as err:
                log("        журнал не принял строку: %s" % err)
    return {"sites": total_sites, "campaigns": len(actions), "ok": ok_n, "failed": bad_n}


def unban(account: Account, sites: Set[str], apply: bool, log: Log,
          mine: Optional[Dict[str, Set[str]]] = None) -> int:
    """Снять площадки, которые запрещал сам робот (mine — его журнал), из своих кампаний.

    Ручной запрет директолога с тем же именем не трогается.
    """
    token, login = account.token(), account.login or ""
    hits = 0
    for campaign in direct.campaigns(token, login, account.campaign_ids):
        items = (campaign.get("ExcludedSites") or {}).get("Items") or []
        own = (mine or {}).get(str(campaign["Id"]), set())
        keep = [s for s in items
                if not (s.strip().lower() in sites and s.strip().lower() in own)]
        if len(keep) == len(items):
            continue
        gone = sorted(set(items) - set(keep))
        log("    %s «%s» −%d: %s" % (campaign["Id"], (campaign.get("Name") or "")[:34],
                                     len(gone), ", ".join(gone)))
        hits += 1
        if apply:
            ok, note = set_excluded_sites(token, login, str(campaign["Id"]), keep)
            if not ok:
                log("        ✗ %s" % note)
    return hits
