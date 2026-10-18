# -*- coding: utf-8 -*-
"""Старый вход чистильщика площадок РСЯ — тонкая обёртка над движком модулей.

    python -m sync.placements_clean --dry-run
    python -m sync.placements_clean --apply
    python -m sync.placements_clean --unban a.ru,b.ru

Расчёт, запись и откат делает раннер (sync.modules.runner, модуль placements);
режим модуля (report/approve/auto) и настройки живут в project_modules, а не
в флагах. Без --apply прогон идёт репетицией: ничего не пишется в базу и Директ.
"""

import argparse
import os
import sys
from datetime import datetime, timezone

from sync.modules import store
from sync.modules.runner import run_project
from sync.placements import accounts as reg
from sync.placements.classify import normalize

MODULE = "placements"
DEFAULT_SLUG = "edunetwork"
WRITE_MODES = ("approve", "auto")


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
        elif account.slug not in seen:
            seen.append(account.slug)
    return seen


def _warn_if_module_idle(slug: str) -> None:
    try:
        rows = {r["module_key"]: r for r in store.load_project_modules(slug)}
    except Exception as err:
        _out("[%s] режим модуля не прочитан (%s)" % (slug, err))
        return
    mode = (rows.get(MODULE) or {}).get("mode")
    if mode not in WRITE_MODES:
        _out("[%s] модуль %s в режиме %s: раннер не выполнит откат, пока модуль не "
             "переведён в approve или auto" % (slug, MODULE, mode or "off"))


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
    slugs = _slugs(keys)
    if not slugs:
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
                hits = [f for f in store.existing_findings(slug, MODULE)
                        if f.status == "applied" and f.object_id in sites]
                for f in hits:
                    _out("    %s / кампания %s / %s" % (f.login, f.campaign_id, f.object_id))
                _out("    итого: %d (для записи добавьте --apply)" % len(hits))
                continue
            n = store.request_rollback(slug, MODULE, sites)
            _out("[%s] поставлено на откат находок: %d" % (slug, n))
            _warn_if_module_idle(slug)
        return 0

    if args.llm:
        os.environ["PLACEMENTS_LLM"] = "1"
    rc = 0
    for slug in slugs:
        rc |= run_project(slug, only=MODULE, now=datetime.now(timezone.utc),
                          dry_run=dry_run, log=_out)
    return rc


if __name__ == "__main__":
    sys.exit(main())
