# -*- coding: utf-8 -*-
"""Такт чистильщика площадок РСЯ.

    python -m sync.placements_clean --dry-run
    python -m sync.placements_clean --accounts russever
    python -m sync.placements_clean --apply

По умолчанию — репетиция: боевая запись включается флагом, потому что
запрет площадки необратим (снятие вернёт трафик, но не вернёт день) и
идёт сразу по нескольким кабинетам.
"""

import argparse
import os
import sys
import traceback

from sync.placements import accounts as reg
from sync.placements import direct, journal, llm
from sync.placements import plan as planner


def _out(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def llm_model(ask=None) -> str:
    return llm.model_name(ask)


def ask_model(plan, ask, use_db: bool):
    """Второй судья по кандидатам плана: (поправки, строка отчёта).

    Дёшево по построению. Модель видит только имена в окне ворот, которые
    словарь оставил обычными сайтами, и только те из них, о которых её ещё
    не спрашивали: вердикт кэшируется навсегда, потому что домен не меняет
    природу. Потолок ASK_LIMIT сдерживает первые прогоны и всплески — остаток
    не теряется, а достаётся следующему часу.
    """
    candidates = plan.get("candidates") or []
    if not candidates:
        return {}, None

    cached = {}
    if use_db:
        try:
            cached = journal.load_llm_verdicts(candidates)
        except Exception as err:
            _out("  кэш вердиктов недоступен: %s" % err)

    unknown = [s for s in candidates if s not in cached][:llm.ASK_LIMIT]
    errors = []
    fresh = llm.judge(unknown, ask=ask, errors=errors)
    if fresh and use_db:
        try:
            journal.save_llm_verdicts(fresh, llm_model(ask))
        except Exception as err:
            _out("  вердикты модели не сохранились: %s" % err)

    overrides = llm.overrides_from(dict(cached, **fresh))
    note = ("  модель: кандидатов %d, из кэша %d, спрошено %d, ответов %d, "
            "к запрету %d"
            % (len(candidates), len(cached), len(unknown), len(fresh),
               len(overrides)))
    if errors:
        # Без этой строки мёртвый провайдер неотличим от чистого кабинета.
        note += ("\n  МОДЕЛЬ НЕ ОТВЕТИЛА (%d раз): %s"
                 % (len(errors), errors[0]))
    return overrides, note


def run_login(account, login: str, apply: bool, top_n: int,
              use_db: bool, ask=None) -> dict:
    token = account.token()
    _out("\n[%s / %s] отчёт площадок за сегодня…" % (account.key, login))
    # Кампании сначала: у агентского кабинета половина клиентов пустая или
    # выключена, и заказывать по ним отчёт — минуты прогона впустую.
    campaigns = direct.campaigns(token, login)
    live = [c for c in campaigns
            if c.get("Type") in direct.CLEANABLE_TYPES
            and c.get("State") in direct.CLEANABLE_STATES]
    if not live:
        _out("  нечего чистить: ни одной запущенной кампании с рычагом")
        return {"sites": 0, "campaigns": 0, "ok": 0, "failed": 0}
    rows = direct.placements_today(token, login)
    plan = planner.plan_account(rows, campaigns, top_n=top_n)

    if ask is not None:
        overrides, note = ask_model(plan, ask, use_db)
        if note:
            _out(note)
        if overrides:
            plan = planner.plan_account(rows, campaigns, top_n=top_n,
                                        overrides=overrides)

    _out("  площадок за день: %d, кликов %d, расход %.0f ₽"
         % (plan["day_sites"], plan["day_clicks"], plan["day_cost"]))
    for verdict in sorted(plan["summary"],
                          key=lambda k: -plan["summary"][k]["cost"]):
        s = plan["summary"][verdict]
        _out("    %-5s %5d площ  %6d кл  %9.0f ₽"
             % (verdict, s["sites"], s["clicks"], s["cost"]))

    actions = plan["actions"]
    total_sites = sum(len(a["added"]) for a in actions)
    _out("  к запрету: %d площадок в %d кампаниях (%d кл, %.0f ₽)"
         % (total_sites, len(actions),
            sum(a["cut_clicks"] for a in actions),
            sum(a["cut_cost"] for a in actions)))
    for item in plan["refused"][:10]:
        # Деньги в отказе показываем всегда: недостижимая кампания молча
        # съедает бюджет, и без цифры это выглядит рядовой технической
        # строкой, а не решением, которое человеку надо принять.
        money = ("  [%d кл, %.0f ₽]" % (item["clicks"], item["cost"])
                 if item.get("clicks") else "")
        _out("    отказ: %s%s" % (item.get("reason"), money))

    # Журнал важен, но чистка важнее: недоступная база не повод оставить
    # кабинет грязным. Отказ журнала виден в выводе и не глушится.
    run_id = None
    if use_db:
        try:
            run_id = journal.start_run(account.key, login, not apply, plan)
        except Exception as err:
            _out("  ЖУРНАЛ НЕДОСТУПЕН, пишем вслепую: %s" % err)

    applied_ok, applied_bad = 0, 0
    for action in actions:
        head = "    %s «%s» +%d → %d слотов" % (
            action["campaign_id"], (action["campaign_name"] or "")[:38],
            len(action["added"]), action["fill_after"])
        if not apply:
            _out(head + "  [репетиция]")
            for a in action["added"][:5]:
                _out("        %-42s %4d кл — %s"
                     % (a["placement"][:42], a["clicks"], a["reason"]))
            if run_id:
                try:
                    journal.record_cuts(run_id, account.key, login, action,
                                        False)
                except Exception as err:
                    _out("        журнал не принял строку: %s" % err)
            continue
        ok, note = direct.set_excluded_sites(token, login,
                                             action["campaign_id"],
                                             action["sites"])
        if ok:
            applied_ok += 1
            _out(head + ("  ✓ " + note if note else "  ✓"))
        else:
            applied_bad += 1
            _out(head + "  ✗ " + note)
        if run_id:
            try:
                journal.record_cuts(run_id, account.key, login, action, ok)
            except Exception as err:
                _out("        журнал не принял строку: %s" % err)

    return {"sites": total_sites, "campaigns": len(actions),
            "ok": applied_ok, "failed": applied_bad}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Чистильщик площадок РСЯ")
    parser.add_argument("--apply", action="store_true",
                        help="боевая запись (по умолчанию репетиция)")
    parser.add_argument("--dry-run", action="store_true",
                        help="явная репетиция; перебивает --apply")
    parser.add_argument("--accounts", default="",
                        help="кабинеты через запятую; пусто — все доступные")
    parser.add_argument("--top-n", type=int, default=planner.TOP_N,
                        help="сколько верхних по кликам смотреть в кампании")
    parser.add_argument("--llm", action="store_true",
                        help="спрашивать модель по кандидатам (нужен ключ)")
    parser.add_argument("--no-db", action="store_true",
                        help="не писать журнал (нет DATABASE_URL)")
    args = parser.parse_args(argv)

    apply = args.apply and not args.dry_run
    keys = [k.strip() for k in args.accounts.split(",") if k.strip()] or None
    use_db = not args.no_db and bool(os.environ.get("DATABASE_URL"))

    ready, skipped = reg.available(keys)
    for account, why in skipped:
        _out("пропуск %s (%s): %s" % (account.key, account.label, why))
    if not ready:
        _out("нет ни одного доступного кабинета")
        return 1

    if use_db:
        try:
            journal.ensure_tables()
        except Exception as err:
            _out("журнал недоступен (%s) — прогон идёт без него" % err)
            use_db = False

    # Второй судья по умолчанию выключен: провайдер платный, и без
    # явного согласия такт за него не платит.
    ask = llm.asker() if args.llm else None
    _out("режим: %s, кабинетов %d, ворота топ-%d по кликам, второй судья: %s"
         % ("БОЕВОЙ" if apply else "репетиция", len(ready), args.top_n,
            ("модель %s" % llm_model(ask)) if ask else "выключен (нужен --llm)"))

    failures = 0
    totals = {"sites": 0, "campaigns": 0, "ok": 0, "failed": 0}
    for account, logins in ready:
        for login in logins:
            try:
                res = run_login(account, login, apply, args.top_n,
                                use_db, ask=ask)
                for key in totals:
                    totals[key] += res[key]
            except Exception as err:  # кабинет не должен ронять остальные
                failures += 1
                _out("  ОШИБКА [%s / %s]: %s" % (account.key, login, err))
                traceback.print_exc()

    _out("\nитого: %d площадок в %d кампаниях; записано кампаний %d, "
         "отказов %d, кабинетов с ошибкой %d"
         % (totals["sites"], totals["campaigns"], totals["ok"],
            totals["failed"], failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
