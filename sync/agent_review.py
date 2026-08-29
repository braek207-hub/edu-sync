# -*- coding: utf-8 -*-
"""
sync/agent_review.py — недельный разбор беты.

Читает чёрный ящик за период (прогоны всех стадий и журнал отказов) и
печатает НАХОДКИ: стены, в которые агент упирается день за днём; конфликты
рычагов, повторяющиеся из прогона в прогон; объекты, чьи изменения человек
возвращает руками; молчащие стадии. Разбор сам ничего не чинит и никуда,
кроме чёрного ящика, не пишет — его продукт это список того, что чинить.

Запуск:
    python -m sync.agent_review               # 7 дней
    python -m sync.agent_review --days=30
    python -m sync.agent_review --fail-on-high   # находки веса high = красный ран
ENV: DATABASE_URL
"""

import json
import sys
from typing import Any, Dict, List

from sync.agent import blackbox, review
from sync.agent import scope as agent_scope
from sync.db import get_connection

DEFAULT_DAYS = 7

# Стадии, чьё молчание — находка. Список явный: отсутствие данных нельзя
# вывести из данных, а молчащий крон выглядит ровно как «всё хорошо».
EXPECTED_STAGES = ("e0", "e1", "watchdog", "drift")

RUNS_SQL = """
    SELECT run_id, stage, mode, started_at, code_sha, run_url, verdict, report
      FROM edu_agent_runs
     WHERE started_at >= now() - make_interval(days => %(days)s)
     ORDER BY started_at
"""

REJECTS_SQL = """
    SELECT run_id, stage, account, object_id, kind, key, reason,
           cost_rub, risk_rub, created_at
      FROM edu_agent_rejects
     WHERE created_at >= now() - make_interval(days => %(days)s)
"""


def _fetch(sql: str, days: int) -> List[Dict[str, Any]]:
    import psycopg2.extras

    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, {"days": int(days)})
            return [dict(row) for row in cur.fetchall()]


# Сколько находок печатается целиком. Полный список уезжает в чёрный ящик:
# лог читает человек, а сравнивать разборы между собой будет запрос.
PRINT_LIMIT = 40


def own_rejects(rejects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Журнал отказов своих кабинетов (sync/agent/scope.py).

    Разбор ищет стены, в которые агент упирается день за днём. Отказ по
    кабинету, который агент вести не должен, такой стеной не является: он
    поднял бы находку про кабинет, где чинить нечего.
    """
    return [r for r in rejects if not agent_scope.is_excluded_account(r.get("account"))]


def main() -> int:
    days = DEFAULT_DAYS
    for arg in sys.argv[1:]:
        if arg.startswith("--days="):
            days = max(1, int(arg.split("=", 1)[1]))

    runs = _fetch(RUNS_SQL, days)
    rejects = own_rejects(_fetch(REJECTS_SQL, days))
    result = review.review(runs, rejects, EXPECTED_STAGES)

    high = result["by_severity"]["high"]
    out = {
        "verdict": "FINDINGS" if result["findings"] else "GREEN",
        "days": days,
        "runs": result["runs"],
        "rejects": result["rejects"],
        "by_code": result["by_code"],
        "by_severity": result["by_severity"],
        "findings": result["findings"][:PRINT_LIMIT],
        # Прогоны по стадиям — контекст, без которого «находок нет» нельзя
        # отличить от «данных нет».
        "runs_by_stage": {stage: sum(1 for r in runs if r.get("stage") == stage)
                          for stage in sorted({str(r.get("stage")) for r in runs})},
    }
    out["blackbox"] = blackbox.save_run(
        blackbox.new_run_id(), stage="review", mode=blackbox.MODE_COMPUTE,
        report={**out, "findings": result["findings"]})
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    if high and "--fail-on-high" in sys.argv:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
