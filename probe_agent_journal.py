# -*- coding: utf-8 -*-
"""
probe_agent_journal.py — что лежит в журнале действий агента.

Живые тесты движка записи пишут реальные строки в edu_agent_actions и убирают за
собой. Проверка нужна по двум причинам: убедиться, что уборка сработала, и что в
журнале нет чужого мусора — иначе сторож красных линий возьмёт его в работу и
попытается «откатывать» то, чего никогда не было в кабинете.

Только чтение. Запуск: python probe_agent_journal.py
ENV: DATABASE_URL
"""

import json

from sync.agent.writer import db as writer_db


def main() -> int:
    writer_db.ensure_writer_tables()

    rows = writer_db._fetch(
        """
        SELECT status,
               account,
               COUNT(*) AS cnt,
               MIN(created_at)::date AS first_day,
               MAX(created_at)::date AS last_day
        FROM edu_agent_actions
        GROUP BY status, account
        ORDER BY cnt DESC
        """
    )
    open_rows = writer_db.open_actions()
    budget = writer_db._fetch("SELECT * FROM edu_agent_risk_budget ORDER BY week_start DESC LIMIT 5")

    # Отклонённые элементы: САМ ТЕКСТ ошибки API. В отчёте прогона его нет —
    # там только счётчик rejected, — а без причины непонятно, чинить настройку,
    # справочник значений или права токена.
    rejected = writer_db._fetch(
        """
        SELECT account, object_id, direct_type, setting_key,
               created_at::date AS day, response
        FROM edu_agent_actions
        WHERE status = 'rejected'
        ORDER BY created_at DESC
        LIMIT 20
        """
    )

    print(json.dumps({
        "rejected_recent": rejected,
        "journal_by_status_account": rows,
        "open_actions_count": len(open_rows),
        "open_actions_sample": [
            {k: str(v)[:80] for k, v in r.items() if k in
             ("action_id", "account", "object_id", "action_kind", "status")}
            for r in open_rows[:10]
        ],
        "risk_budget_rows": budget,
    }, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
