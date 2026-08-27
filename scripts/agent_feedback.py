#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/agent_feedback.py — исход запущенной кампании уезжает билдеру.

Зачем. Кампанию по наряду агента собрал и залил другой репозиторий (билдер,
d:/vscode/EDU кампании). Он умеет дообучаться на боевых поисковых запросах, но
про кампанию знает только клики и расход, а расход — не исход: вынесенные
связки могли потратить ровно столько же и при этом побить донорскую цену
конверсии или провалить её. Вердикт считает агент — критерием, который сам же
выписал в наряде, — и отдаёт его вместе с сырыми запросами.

Что делает. Читает наряд из реестра идей по order_id, считает отчёт
(sync/agent/ideas/feedback_out.py) и печатает его JSON-ом. Ничего не пишет ни
в базу, ни в кабинет: это чтение.

    python scripts/agent_feedback.py --list
    python scripts/agent_feedback.py consolidate-vpo --out отчёт.json

Дальше отчёт читает билдер — вместе с нарядом, по которому уровень собирался
(уровня агента нет в реестре LEVELS, он придуман в рантайме):

    python -m builder.feedback --report отчёт.json --order наряд.json --apply

ENV: DATABASE_URL
"""

import argparse
import json
import sys
from pathlib import Path

# Корень репозитория в пути: при запуске «python scripts/agent_feedback.py»
# в sys.path попадает scripts/, и пакет sync не находится (образец —
# scripts/reprice_actions.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sync.agent.ideas import feedback_out, registry  # noqa: E402

LAUNCH_KIND = "campaign.create"


def orders(account=None):
    """Наряды открытых идей: order_id → имя кампании.

    Только открытые — это список «что можно спросить сейчас». Закрытую идею
    отчёт тоже найдёт (registry.find_by_order смотрит все статусы), но
    показывать в перечне историю целиком незачем: спрашивают по свежему.
    """
    out = []
    for idea in registry.open_ideas(account=account):
        action = idea.get("action") or {}
        if str(action.get("kind") or "") != LAUNCH_KIND:
            continue
        order = (action.get("payload") or {}).get("order") or {}
        if order.get("order_id"):
            out.append((str(order["order_id"]), str(order.get("campaign_name") or ""),
                        str(idea.get("account") or "")))
    return out


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("order_id", nargs="?", help="наряд, по которому нужен исход")
    ap.add_argument("--account", default=None)
    ap.add_argument("--today", default=None, help="день отсчёта, ГГГГ-ММ-ДД")
    ap.add_argument("--list", action="store_true", help="какие наряды открыты")
    ap.add_argument("--out", help="файл отчёта; без него — на экран")
    args = ap.parse_args()

    if args.list or not args.order_id:
        rows = orders(account=args.account)
        if not rows:
            print("Открытых нарядов нет.")
            return 0
        for order_id, campaign, account in rows:
            print(f"{order_id:<32} {account:<24} {campaign}")
        return 0

    report = feedback_out.for_order(args.order_id, today=args.today,
                                    account=args.account)
    if report is None:
        print(f"Наряда {args.order_id!r} в реестре нет.")
        return 1

    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8", newline="\n")
        print(f"{report['verdict']}: {report['reason']}")
        print(f"Отчёт: {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
