#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/rollback_list_actions.py — ручной откат списочных действий.

Автооткат (writer/rollback.py) списки не умеет: rollback_payload строит
возврат для корректировок, стратегий, расписаний — а negative.add и
placement.exclude возвращает None. Дыру вскрыл первый разбор аналитика
03.09 (rule_issues №5) вместе с двумя действиями, которые Павел велел
откатить: минус-фраза «экономика» (название направления подготовки, не
мусор) и mail.ru у кампании без собственной статистики.

Почему НЕ previous_state целиком: списки в API заменяются полностью, а
между применением и откатом сюда могли доехать чужие добавления (вчерашняя
гигиена, завтрашний прогон). Возврат прежнего списка стёр бы их. Поэтому
откат точечный: читаем ТЕКУЩИЙ список кабинета, вычитаем ровно то, что
добавило само действие (payload минус previous_state), пишем обратно.

    python scripts/rollback_list_actions.py --ids=a,b,c            # план
    python scripts/rollback_list_actions.py --ids=a,b,c --apply --prod

ENV: DATABASE_URL, DIRECT_TOKEN (боевой — только в Actions).
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sync.agent.writer import db as writer_db  # noqa: E402
from sync.agent.writer.client import WriteClient  # noqa: E402

LIST_FIELDS = {
    "negative.add": "NegativeKeywords",
    "placement.exclude": "ExcludedSites",
}


def _items(block: Any) -> List[str]:
    """Items из блока, который бывает и {'Items': [...]}, и просто списком."""
    if isinstance(block, dict):
        return [str(x) for x in (block.get("Items") or [])]
    if isinstance(block, list):
        return [str(x) for x in block]
    return []


def added_items(action: Dict[str, Any], field: str) -> List[str]:
    """Ровно то, что добавило действие: план минус прежнее состояние."""
    payload = action.get("payload") or {}
    previous = set(_items((action.get("previous_state") or {}).get(field)))
    return [x for x in _items(payload.get(field)) if x not in previous]


def load_action(action_id: str) -> Optional[Dict[str, Any]]:
    import psycopg2.extras

    from sync.db import get_connection

    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM edu_agent_actions WHERE action_id = %s",
                        (action_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def rollback_one(client: WriteClient, action: Dict[str, Any]) -> Dict[str, Any]:
    field = LIST_FIELDS.get(str(action.get("action_kind")))
    base = {"action_id": action.get("action_id"),
            "object_id": action.get("object_id"), "field": field}
    if field is None:
        return {**base, "result": "unsupported_kind",
                "kind": action.get("action_kind")}
    if str(action.get("status")) != "applied" or action.get("rolled_back_at"):
        return {**base, "result": "not_applied_or_already_rolled_back",
                "status": action.get("status")}

    campaign_id = int((action.get("payload") or {}).get("CampaignId")
                      or action.get("object_id"))
    added = added_items(action, field)
    if not added:
        return {**base, "result": "nothing_added"}

    # client.get отдаёт уже развёрнутый result (см. WriteClient._call).
    got = client.get("campaigns", {
        "SelectionCriteria": {"Ids": [campaign_id]},
        "FieldNames": ["Id", field],
    })
    campaigns = (got or {}).get("Campaigns") or []
    if not campaigns:
        return {**base, "result": "campaign_not_found"}
    current = _items(campaigns[0].get(field))
    remaining = [x for x in current if x not in set(added)]
    removed = [x for x in added if x in set(current)]
    if not removed:
        return {**base, "result": "already_absent", "added": added}

    plan = {**base, "removed": removed, "kept": len(remaining)}
    if not client.is_write_allowed():
        return {**plan, "result": "dry_run"}

    response = client.mutate("campaigns", "update", {
        "Campaigns": [{"Id": campaign_id, field: {"Items": remaining}}]
    })
    # Ошибки уровня элемента приходят внутри result, а не исключением.
    item_errors = [e for r in (response or {}).get("UpdateResults", [])
                   for e in (r.get("Errors") or [])]
    if item_errors:
        return {**plan, "result": "api_element_error", "errors": item_errors}
    marked = writer_db.mark_rolled_back(action["action_id"])
    return {**plan, "result": "rolled_back", "journal_marked": bool(marked)}


def main() -> int:
    ids: List[str] = []
    sandbox = "--prod" not in sys.argv
    dry_run = "--apply" not in sys.argv
    for arg in sys.argv[1:]:
        if arg.startswith("--ids="):
            ids = [x.strip() for x in arg.split("=", 1)[1].split(",") if x.strip()]
    if not ids:
        print(json.dumps({"error": "нет --ids"}, ensure_ascii=False))
        return 2

    results: List[Dict[str, Any]] = []
    # Та же аренда, что у e1 и сторожа: параллельная запись в один кабинет
    # и один журнал — гонка.
    with writer_db.run_lock("agent_writer"):
        clients: Dict[str, WriteClient] = {}
        for action_id in ids:
            action = load_action(action_id)
            if action is None:
                results.append({"action_id": action_id, "result": "not_found"})
                continue
            login = str(action.get("account") or "")
            if login not in clients:
                clients[login] = WriteClient(login, sandbox=sandbox, dry_run=dry_run)
            try:
                results.append(rollback_one(clients[login], action))
            except Exception as exc:  # noqa: BLE001 — остальные ids должны доехать
                results.append({"action_id": action_id, "result": "error",
                                "reason": f"{type(exc).__name__}: {exc}"[:300]})

    out = {"sandbox": sandbox, "dry_run": dry_run, "results": results}
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0 if all(r.get("result") in ("rolled_back", "dry_run", "already_absent",
                                        "nothing_added") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
