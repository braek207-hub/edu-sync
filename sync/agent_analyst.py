# -*- coding: utf-8 -*-
"""
sync/agent_analyst.py — ежедневный надзор модели над решениями агента.

Читает выхлоп агента за сутки (прогоны, действия, отказы, идеи, панель,
риск-бюджет), отдаёт Claude и получает четыре вердикта: разбор дня,
вето-кандидаты, возможности сверх гейтов, дефекты правил. Этап 1 —
READ-ONLY: вердикты уходят владельцу в Telegram и в чёрный ящик, в
кабинет и панель не идёт ничего (почему так — sync/agent/analyst.py).

Запуск:
    python -m sync.agent_analyst            # сутки
    python -m sync.agent_analyst --days=3
ENV: DATABASE_URL, ANTHROPIC_API_KEY (нет ключа — SKIPPED, не падение),
     TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID (нет — разбор только в ящик).
"""

import json
import sys
from typing import Any, Dict, List

from sync.agent import analyst, blackbox, notify
from sync.db import get_connection

DEFAULT_DAYS = 1

RUNS_SQL = """
    SELECT stage, mode, started_at, verdict, report
      FROM edu_agent_runs
     WHERE started_at >= now() - make_interval(days => %(days)s)
     ORDER BY started_at
"""

ACTIONS_SQL = """
    SELECT action_id, account, object_level, object_id, action_kind,
           payload, previous_state, risk_rub, status
      FROM edu_agent_actions
     WHERE created_at >= now() - make_interval(days => %(days)s)
     ORDER BY created_at
"""

# Отказы сразу группами: поштучный журнал за сутки — тысячи строк, а
# суждению нужны стены, то есть повторы одной причины.
REJECTS_SQL = """
    SELECT stage, kind, reason, count(*) AS count,
           round(sum(coalesce(cost_rub, 0))::numeric) AS cost_rub,
           (array_agg(key))[1:5] AS sample_keys
      FROM edu_agent_rejects
     WHERE created_at >= now() - make_interval(days => %(days)s)
     GROUP BY stage, kind, reason
     ORDER BY count(*) DESC
"""

IDEAS_SQL = """
    SELECT source, lane, tier, status, subject_key,
           expected_rub, horizon_days, dropped_reason
      FROM edu_agent_ideas
     WHERE updated_at >= now() - make_interval(days => 7)
     ORDER BY updated_at DESC
"""

CONFIG_SQL = """
    SELECT key, value, updated_at, updated_by
      FROM edu_agent_config
     ORDER BY key
"""

RISK_SQL = """
    SELECT week_start, limit_rub
      FROM edu_agent_risk_budget
     ORDER BY week_start DESC
     LIMIT 1
"""


def _fetch(sql: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    import psycopg2.extras

    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]


def gather_context(days: int) -> Dict[str, Any]:
    from datetime import date

    return {
        "as_of": date.today().isoformat(),
        "runs": analyst.compact_runs(_fetch(RUNS_SQL, {"days": days})),
        "actions": analyst.compact_actions(_fetch(ACTIONS_SQL, {"days": days})),
        "rejects": analyst.compact_rejects(_fetch(REJECTS_SQL, {"days": days})),
        "ideas": analyst.compact_ideas(_fetch(IDEAS_SQL, {})),
        "config": _fetch(CONFIG_SQL, {}),
        "risk_budget": ( _fetch(RISK_SQL, {}) or [{}])[0],
    }


def ask_model(system: str, user: str) -> Dict[str, Any]:
    """Один вызов Claude. Любой отказ — {'raw': None, 'error': ...}:
    надзор не вправе уронить ни один прогон, включая собственный."""
    import os

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"raw": None, "error": "no_api_key", "usage": None}
    try:
        import anthropic

        client = anthropic.Anthropic()
        response = client.messages.create(
            model=analyst.MODEL, max_tokens=analyst.MAX_TOKENS,
            system=system, messages=[{"role": "user", "content": user}])
        text = "".join(b.text for b in response.content
                       if getattr(b, "type", "") == "text")
        usage = {"input_tokens": response.usage.input_tokens,
                 "output_tokens": response.usage.output_tokens}
        return {"raw": text, "error": None, "usage": usage}
    except Exception as exc:  # noqa: BLE001 — см. докстринг
        return {"raw": None, "error": f"{type(exc).__name__}: {exc}"[:300],
                "usage": None}


def main() -> int:
    days = DEFAULT_DAYS
    for arg in sys.argv[1:]:
        if arg.startswith("--days="):
            days = max(1, int(arg.split("=", 1)[1]))

    context = gather_context(days)
    answer = ask_model(analyst.SYSTEM_PROMPT, analyst.build_user_prompt(context))
    result = analyst.parse_response(answer["raw"]) if answer["raw"] else None

    if result is None:
        out: Dict[str, Any] = {
            "verdict": analyst.VERDICT_SKIPPED,
            "days": days,
            "error": answer["error"] or "unparseable_response",
            "usage": answer["usage"],
        }
        telegram = {"sent": False, "reason": "skipped"}
    else:
        message = analyst.format_telegram(result)
        telegram = notify.send(message)
        out = {
            "verdict": analyst.VERDICT_OK,
            "days": days,
            "digest": result["digest"],
            "would_veto": result["would_veto"],
            "beyond_gates": result["beyond_gates"],
            "rule_issues": result["rule_issues"],
            "usage": answer["usage"],
        }
    out["telegram"] = telegram
    out["blackbox"] = blackbox.save_run(
        blackbox.new_run_id(), stage="analyst", mode=blackbox.MODE_COMPUTE,
        report=out)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
