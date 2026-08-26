#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/reprice_actions.py — перевод журнала действий в цены дельта-модели.

Зачем. Дельта-модель риска (sync/agent/writer/exposure.py) появилась
25.08.2026, а строки журнала до неё оценены «весь дневной расход кампании ×
горизонт замера». Пять строк от 22.08 держат 38 876 ₽ из 50 000 недельного
лимита и держали бы до 05.09: writer/db.spent_risk расширяет окно недели назад
на горизонт замера. Пока журнал в старых ценах, агент почти не может писать.

Что делает. По умолчанию НИЧЕГО не пишет: печатает план переоценки (что было,
что станет, на каком основании) и ОТДЕЛЬНО — строки, которые переоценить не
удалось, с причиной по каждой. Молчаливое «оставили как есть» неотличимо от
«проверили и всё верно», поэтому второй список обязателен даже когда он
длиннее первого.

С --apply пишет новую цену только в строки за последние 28 дней, которые
РЕАЛЬНО ЗАНИМАЮТ недельный лимит, — writer_db.RISK_CHARGED_STATUSES. Это не
то же, что LIVE_STATUSES: откатанная строка тоже платит риском, потому что
экспозиция уже случилась и откат не возвращает деньги, потраченные за дни до
него (см. spent_risk). Возьми фильтр по LIVE_STATUSES — и откатанные строки в
старых ценах продолжили бы держать бюджет, а переоценка бы их не достала.
Строки старше окна лимит уже не занимают: spent_risk считает неделю.

Запуск: python scripts/reprice_actions.py [--apply] [--days 28]
ENV: DATABASE_URL
"""

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Корень репозитория в пути: при запуске «python scripts/reprice_actions.py»
# в sys.path попадает scripts/, и пакет sync не находится (образец —
# scripts/run-direct-backfill.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sync.agent import db as agent_db  # noqa: E402
from sync.agent.writer import db as writer_db  # noqa: E402
from sync.agent.writer import reprice  # noqa: E402
from sync.db import get_connection  # noqa: E402

DEFAULT_WINDOW_DAYS = 28

# Окно, по которому оценивается дневной расход кампании, — то же, что у
# прогона (sync/agent_e1.py::cutoff). Другое окно дало бы другую цену при той
# же модели, и переоценённая строка разошлась бы со строкой следующего прогона.
COST_WINDOW_DAYS = 30

SELECT_CHARGED_SQL = """
    SELECT action_id, account, object_level, object_id, action_kind,
           direct_type, setting_key, payload, risk_rub, risk_basis,
           status, applied_at
    FROM edu_agent_actions
    WHERE status IN (__CHARGED_STATUSES__)
      AND applied_at >= now() - make_interval(days => %s)
    ORDER BY applied_at
""".replace("__CHARGED_STATUSES__",
            writer_db._sql_literals(writer_db.RISK_CHARGED_STATUSES))

# Пишется и основание цены. Пустой risk_basis — это и есть признак «строку
# оценивала модель до дельты» (колонка появилась вместе с ней, и по ней же
# reprice.plan отличает уже пересчитанное). Записать новую цену, оставив
# основание пустым, значило бы сделать переоценённую строку неотличимой от
# непереоценённой — то есть повторить ровно ту неоднозначность, ради снятия
# которой этот скрипт написан.
UPDATE_SQL = ("UPDATE edu_agent_actions SET risk_rub = %s, risk_basis = %s "
              "WHERE action_id = %s")


def _daily_cost_on(day: date, cache: Dict[date, Dict[str, float]]) -> Dict[str, float]:
    """Дневной расход кампаний, каким он был на дату действия.

    Не сегодняшний: расход кампании после корректировки — уже следствие
    той самой правки, которую мы переоцениваем, тем же доводом, по которому
    доля сегмента берётся из расчёта того дня.
    """
    if day not in cache:
        cutoff = (day - timedelta(days=COST_WINDOW_DAYS)).isoformat()
        cache[day] = agent_db.load_daily_cost_by_campaign(cutoff, day.isoformat())
    return cache[day]


def build_plan(rows: List[Dict[str, Any]]
               ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Строки журнала → (что переоценить, что осталось как есть с причиной).

    Доли и расход читаются на дату КАЖДОЙ строки, поэтому строки группируются
    по (кабинет, объект, дата): один запрос на группу вместо запроса на строку.
    """
    repriced: List[Dict[str, Any]] = []
    untouched: List[Dict[str, Any]] = []
    groups: Dict[Tuple[str, str, date], List[Dict[str, Any]]] = {}
    for row in rows:
        day = reprice.action_date(row)
        if day is None:
            untouched.append({"action_id": row.get("action_id"),
                              "action_kind": row.get("action_kind"),
                              "object_id": row.get("object_id"),
                              "risk_rub": float(row.get("risk_rub") or 0.0),
                              "reason": "у строки нет даты — расчёт того дня не выбрать"})
            continue
        groups.setdefault((str(row.get("account")), str(row.get("object_id")), day),
                          []).append(row)

    cost_cache: Dict[date, Dict[str, float]] = {}
    for (account, object_id, day), group in sorted(groups.items(), key=lambda kv: kv[0][2]):
        computed = reprice.computed_for_action(account, object_id, day)
        part_repriced, part_untouched = reprice.plan(
            group, computed, _daily_cost_on(day, cost_cache))
        repriced += part_repriced
        untouched += part_untouched
    return repriced, untouched


def apply_plan(repriced: List[Dict[str, Any]]) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            for row in repriced:
                cur.execute(UPDATE_SQL,
                            (row["new"], row["basis"], row["action_id"]))
        conn.commit()
    return len(repriced)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="записать новые цены (без флага — только план)")
    parser.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS,
                        help="глубина окна по applied_at, дней")
    args = parser.parse_args()

    rows = writer_db._fetch(SELECT_CHARGED_SQL, (int(args.days),))
    if not rows:
        print(f"строк под риском за {args.days} дн. нет — переоценивать нечего")
        return 0

    repriced, untouched = build_plan(rows)

    print(f"строк под риском в окне {args.days} дн.: {len(rows)}")
    print(f"\nпереоценить ({len(repriced)}):")
    for row in repriced:
        print(f"  {row['action_id']}  {row['action_kind']}  "
              f"кампания {row['object_id']}  "
              f"{row['old']:.2f} → {row['new']:.2f} ₽  ({row['basis']})")

    print(f"\nоставлено как есть ({len(untouched)}):")
    for row in untouched:
        print(f"  {row['action_id']}  {row['action_kind']}  "
              f"кампания {row['object_id']}  {row['risk_rub']:.2f} ₽  "
              f"— {row['reason']}")

    freed = sum(row["old"] - row["new"] for row in repriced)
    print(f"\nвысвобождается из недельного риск-бюджета: {freed:.2f} ₽")

    if not args.apply:
        print("\nплан не применён (нет --apply)")
        return 0

    print(f"\nзаписано строк: {apply_plan(repriced)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
