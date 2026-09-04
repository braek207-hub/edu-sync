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
    python -m sync.agent_review --notify         # + сводка в Telegram
ENV: DATABASE_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (для --notify)
"""

import json
import sys
from typing import Any, Dict, List

from sync.agent import blackbox, notify, review
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


# Счётчики действий за окно. Применённое считается по applied_at, а
# ожидающее/отклонённое/откаченное — по created_at: «применено за неделю»
# человек понимает как «применилось на этой неделе», а не «родилось на ней».
# Предвыборка по created_at за 30 дней до окна держит запрос по индексу:
# применение позже создания, но не на месяцы.
ACTIONS_SQL = """
    SELECT
      count(*) FILTER (WHERE status = 'applied'
                         AND applied_at >= {since} AND applied_at < {until})
        AS applied,
      COALESCE(sum(risk_rub) FILTER (WHERE status = 'applied'
                         AND applied_at >= {since} AND applied_at < {until}), 0)
        AS risk_rub,
      count(*) FILTER (WHERE status = 'pending_approval'
                         AND created_at >= {since} AND created_at < {until})
        AS pending_approval,
      count(*) FILTER (WHERE status = 'rejected'
                         AND created_at >= {since} AND created_at < {until})
        AS rejected,
      count(*) FILTER (WHERE status = 'rolled_back'
                         AND created_at >= {since} AND created_at < {until})
        AS rolled_back
      FROM edu_agent_actions
     WHERE created_at >= {since} - interval '30 days'
"""

# Выгода в рублях не пересчитывается разбором: её считает Э0 и кладёт в свой
# отчёт (sync/agent/value.py:period_value). Пересчёт здесь дал бы вторую
# правду о тех же деньгах, расходящуюся с экраном агента.
VALUE_SQL = """
    SELECT report -> 'agent_value' AS value
      FROM edu_agent_runs
     WHERE stage = 'e0'
       AND started_at >= now() - make_interval(days => %(days)s)
       AND report ? 'agent_value'
     ORDER BY started_at DESC
     LIMIT 1
"""


def _fetch(sql: str, days: int) -> List[Dict[str, Any]]:
    import psycopg2.extras

    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, {"days": int(days)})
            return [dict(row) for row in cur.fetchall()]


def _fetch_one(sql: str, params: Dict[str, Any]) -> Dict[str, Any]:
    import psycopg2.extras

    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else {}


def week_counters(days: int) -> Dict[str, Dict[str, Any]]:
    """Действия текущего окна и предыдущего такой же длины — для «больше или
    меньше, чем неделей раньше». Границы считает Postgres: разбор и база
    обязаны понимать «неделю назад» одинаково, а now() в Python — это часовой
    пояс раннера GitHub, то есть UTC, а не тот же момент.
    """
    span = f"now() - make_interval(days => {int(days)})"
    prev = f"now() - make_interval(days => {int(days) * 2})"
    # Границы окна — выражения SQL, а не значения, поэтому подставляются
    # format-ом. Снаружи в них попадает только int(days): места для строки
    # из аргументов запуска здесь нет.
    now_row = _fetch_one(ACTIONS_SQL.format(since=span, until="now()"), {})
    prev_row = _fetch_one(ACTIONS_SQL.format(since=prev, until=span), {})
    return {"now": now_row, "prev": prev_row}


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

    if "--notify" in sys.argv:
        # Разбор не вправе упасть из-за телеграма: его продукт — список того,
        # что чинить, и он уже напечатан и сохранён выше.
        try:
            counters = week_counters(days)
            value = (_fetch_one(VALUE_SQL, {"days": days}) or {}).get("value")
            text = notify.review_summary(result, value,
                                         counters["now"], counters["prev"], days)
            out["notify"] = notify.send(text)
        except Exception as exc:  # noqa: BLE001
            out["notify"] = {"sent": False,
                             "reason": f"{type(exc).__name__}: {exc}"[:200]}
        print(json.dumps({"notify": out["notify"]}, ensure_ascii=False))

    if high and "--fail-on-high" in sys.argv:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
