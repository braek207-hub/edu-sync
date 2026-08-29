#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/agent_build_queue.py — окно очереди нарядов наружу, для билдера.

Зачем отдельная точка входа. Очередь `edu_agent_build_orders` — единственная
таблица агента, которую читает ЧУЖОЙ репозиторий (билдер, d:/vscode/EDU
кампании). Дай ему ходить в неё своим SQL — и инварианты обмена разъедутся по
двум кодовым базам: законность перехода статуса, заморозка тела взятого наряда
и, главное, заведение наблюдения за созданной кампанией. Последнее вообще не
про билдера: он про кампанию знает клики и расход, а вердикт по горизонту
считает агент. Поэтому ответ билдера входит сюда, а не в его собственный UPDATE.

Что делает. Печатает очередь и двигает статусы вызовами sync/agent/build_queue:

    python scripts/agent_build_queue.py list
    python scripts/agent_build_queue.py list --status built,failed
    python scripts/agent_build_queue.py take consolidate-vpo --by builder
    python scripts/agent_build_queue.py accept consolidate-vpo --campaign-id 555 \
        --started-on 2026-09-03
    python scripts/agent_build_queue.py fail consolidate-vpo --reason "паспорт не покрывает"

`accept` без `--started-on` законен: кампания создаётся на паузе, дня первой
открутки в момент сборки ещё нет. Наблюдение заведётся вторым вызовом с датой.

Форма наряда и ответа — docs/BUILD-ORDER-QUEUE.md.

ENV: DATABASE_URL
"""

import argparse
import json
import sys
from pathlib import Path

# Корень репозитория в пути: при запуске «python scripts/agent_build_queue.py»
# в sys.path попадает scripts/, и пакет sync не находится (образец —
# scripts/agent_feedback.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sync.agent import build_queue  # noqa: E402


def _show(row) -> None:
    print(json.dumps(row, ensure_ascii=False, indent=2, default=str))


def _missing(order_id: str) -> int:
    print(f"Наряда {order_id!r} в очереди нет.")
    return 1


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="что лежит в очереди")
    p_list.add_argument("--status", default=",".join(build_queue.OPEN_STATUSES),
                        help="через запятую; по умолчанию открытые")
    p_list.add_argument("--account", default=None)
    p_list.add_argument("--json", action="store_true",
                        help="целиком, вместе с телами нарядов")

    p_take = sub.add_parser("take", help="билдер берёт наряд в работу")
    p_take.add_argument("order_id")
    p_take.add_argument("--by", required=True, help="кто взял")

    p_ok = sub.add_parser("accept", help="билдер собрал кампанию")
    p_ok.add_argument("order_id")
    p_ok.add_argument("--campaign-id", required=True)
    p_ok.add_argument("--started-on", default=None,
                      help="день ПЕРВОЙ ОТКРУТКИ, ГГГГ-ММ-ДД; не создания")
    p_ok.add_argument("--note", default="")

    p_no = sub.add_parser("fail", help="билдер не смог")
    p_no.add_argument("order_id")
    p_no.add_argument("--reason", required=True)

    args = ap.parse_args()

    if args.cmd == "list":
        statuses = tuple(s.strip() for s in args.status.split(",") if s.strip())
        rows = build_queue.by_status(statuses, account=args.account)
        if not rows:
            print("Пусто.")
            return 0
        if args.json:
            _show(rows)
            return 0
        for row in rows:
            print(f"{row['order_id']:<32} {row['status']:<10} "
                  f"{row.get('campaign_id') or '-':<12} {row['campaign_name']}")
        return 0

    if args.cmd == "take":
        row = build_queue.take(args.order_id, args.by)
    elif args.cmd == "accept":
        row = build_queue.accept(args.order_id, campaign_id=args.campaign_id,
                                 started_on=args.started_on, note=args.note)
    else:
        row = build_queue.fail(args.order_id, args.reason)

    if row is None:
        return _missing(args.order_id)
    _show(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
