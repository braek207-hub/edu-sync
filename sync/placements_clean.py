# -*- coding: utf-8 -*-
"""Старый вход чистильщика площадок РСЯ — тонкая обёртка над движком модулей.

    python -m sync.placements_clean --dry-run
    python -m sync.placements_clean --apply
    python -m sync.placements_clean --unban a.ru,b.ru

Расчёт, запись и откат делает раннер (sync.modules.runner, модуль placements);
режим модуля (report/approve/auto) и настройки живут в project_modules, а не
в флагах. Без --apply прогон идёт репетицией: ничего не пишется в базу и Директ.

Кабинет вне проектов Panda-BI (accounts.legacy_accounts, сейчас ОШ Синергия)
модулю недоступен и чистится прежним движком (sync.placements.legacy) — только
когда назван в --accounts явно:

    python -m sync.placements_clean --apply --accounts synergy_school
"""

import argparse
import os
import sys
import traceback
from datetime import datetime, timezone

from sync.modules import store
from sync.modules.runner import run_project
from sync.placements import accounts as reg
from sync.placements import journal, legacy
from sync.placements.classify import normalize

MODULE = "placements"
DEFAULT_SLUG = "edunetwork"


def _out(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _slugs(keys) -> list:
    if not keys:
        return [DEFAULT_SLUG]
    seen = []
    for key in keys:
        account = reg.BY_KEY.get(key)
        if account is None:
            _out("неизвестный кабинет: %s" % key)
        elif account.slug and account.slug not in seen:
            seen.append(account.slug)
    return seen


def _legacy_db() -> bool:
    if not os.environ.get("DATABASE_URL"):
        return False
    try:
        journal.ensure_tables()
        return True
    except Exception as err:
        _out("журнал недоступен (%s) — прогон идёт без него" % err)
        return False


def _legacy_unban(accounts, sites, write: bool) -> int:
    use_db = _legacy_db()
    for account in accounts:
        mine = {}
        if use_db:
            try:
                mine = journal.robot_cuts(account.key)
            except Exception as err:
                _out("  журнал недоступен: %s" % err)
        if not mine:
            # Без журнала свой запрет неотличим от человеческого.
            _out("[%s] журнала нет — снимать небезопасно, пропуск" % account.key)
            continue
        _out("[%s] снятие (%s)" % (account.key, "БОЕВОЕ" if write else "репетиция"))
        try:
            legacy.unban(account, set(sites), write, _out, mine)
        except Exception as err:
            _out("  ОШИБКА: %s" % err)
            return 1
    return 0


def _legacy_run(accounts, apply: bool) -> int:
    use_db = _legacy_db()
    failures = 0
    for account in accounts:
        try:
            legacy.run_account(account, apply, use_db, _out)
        except Exception as err:  # кабинет не должен ронять остальные
            failures += 1
            _out("  ОШИБКА [%s]: %s" % (account.key, err))
            traceback.print_exc()
    return 1 if failures else 0


def _warn_if_module_idle(slug: str) -> None:
    try:
        rows = {r["module_key"]: r for r in store.load_project_modules(slug)}
    except Exception as err:
        _out("[%s] режим модуля не прочитан (%s)" % (slug, err))
        return
    # откат раннер выполняет в report, approve и auto; не трогает только выключенный модуль
    if MODULE not in rows:
        _out("[%s] модуль %s выключен — откат выполнится после включения" % (slug, MODULE))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Чистильщик площадок РСЯ")
    parser.add_argument("--apply", action="store_true",
                        help="боевой прогон (по умолчанию репетиция)")
    parser.add_argument("--dry-run", action="store_true",
                        help="явная репетиция; перебивает --apply")
    parser.add_argument("--accounts", default="",
                        help="кабинеты через запятую; пусто — edunetwork")
    parser.add_argument("--unban", default="",
                        help="попросить снять эти площадки (через запятую) "
                             "и выйти: снимает раннер; в базу пишет только с --apply")
    parser.add_argument("--llm", action="store_true",
                        help="спрашивать модель по кандидатам (нужен ключ)")
    parser.add_argument("--top-n", type=int, default=None,
                        help="не используется: top_n задаётся настройками модуля")
    parser.add_argument("--no-db", action="store_true",
                        help="не используется: состояние теперь в базе модулей")
    args = parser.parse_args(argv)

    dry_run = args.dry_run or not args.apply
    keys = [k.strip() for k in args.accounts.split(",") if k.strip()]
    old = reg.legacy_accounts(keys) if keys else []
    slugs = _slugs(keys)
    if not slugs and not old:
        return 1
    for flag, used in (("--top-n", args.top_n is not None), ("--no-db", args.no_db)):
        if used:
            _out("%s проигнорирован: настройки модуля берутся из project_modules" % flag)

    if args.unban:
        sites = sorted({normalize(x) for x in args.unban.split(",") if normalize(x)})
        _out("снятие запретов: %s" % ", ".join(sites))
        write = args.apply and not args.dry_run
        for slug in slugs:
            # Запрос касается только находок в статусе applied, то есть записанных
            # самим роботом: ручные запреты директолога в них не входят.
            if not write:
                _out("[%s] репетиция, в базу ничего не пишу; затронуло бы:" % slug)
                try:
                    found = store.existing_findings(slug, MODULE)
                except Exception as err:
                    _out("[%s] база недоступна, список не получен: %s" % (slug, err))
                    return 1
                hits = [f for f in found
                        if f.status == "applied" and f.object_id in sites]
                for f in hits:
                    _out("    %s / кампания %s / %s" % (f.login, f.campaign_id, f.object_id))
                _out("    итого: %d (для записи добавьте --apply)" % len(hits))
                continue
            n = store.request_rollback(slug, MODULE, sites)
            _out("[%s] поставлено на откат находок: %d" % (slug, n))
            _warn_if_module_idle(slug)
        return _legacy_unban(old, sites, write) if old else 0

    if args.llm:
        os.environ["PLACEMENTS_LLM"] = "1"
    rc = _legacy_run(old, not dry_run) if old else 0
    for slug in slugs:
        rc |= run_project(slug, only=MODULE, now=datetime.now(timezone.utc),
                          dry_run=dry_run, log=_out)
    return rc


if __name__ == "__main__":
    sys.exit(main())
