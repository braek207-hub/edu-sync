# -*- coding: utf-8 -*-
"""
sync/agent_approver.py — воркер апрув-контура: читает ответы человека в
Telegram и исполняет их.

Полный разбор контура — sync/agent/approval.py. Здесь только исполнение:

  1. Просроченные pending (старше PENDING_TTL_HOURS) закрываются сами:
     молчание = «нет», расчёт двухдневной давности не применяется.
  2. getUpdates с курсором (approval_db) → решения человека из ЕГО чата
     (TELEGRAM_CHAT_ID). Чужие сообщения игнорируются молча.
  3. «нет» → rejected(human_veto) + память вето (VETO_MEMORY_DAYS дней
     план не ставит этот ключ в очередь заново).
  4. «да» → применение той же машиной статусов, что у Э1: строка
     возвращается в planned (claim_for_apply), mark_sent, mutate,
     разбор ошибок уровня элемента, mark_action. Исключение после
     отправки — mark_unknown_outcome: строка уходит под сторожа, как
     любое зависшее изменение.

Аренда: та же run_lock("agent_writer"), что у Э1 и сторожа, — параллельная
запись в один кабинет и один журнал недопустима. Занято — выходим молча,
следующий крон доберёт (решения человека лежат в чате, ничего не теряется).

Запуск: GitHub Actions (agent-approver.yml), крон каждые 20 минут днём.
    python -m sync.agent_approver            # боевой
    python -m sync.agent_approver --dry-run  # разбор без записи в кабинет
"""

import json
import os
import sys
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from sync.agent import approval, approval_db, notify
from sync.agent.writer import apply as writer_apply
from sync.agent.writer import db as writer_db
from sync.agent.writer.client import DirectWriteError, WriteClient, is_outcome_unknown

GETUPDATES_URL = "https://api.telegram.org/bot{token}/getUpdates"


def _print(obj: Dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def fetch_updates(offset: int) -> List[Dict[str, Any]]:
    """getUpdates от курсора. Отказ транспорта — пустой список: решения
    человека лежат в чате и никуда не денутся до следующего крона."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return []
    url = (GETUPDATES_URL.format(token=token)
           + f"?offset={int(offset) + 1}&timeout=0&allowed_updates=%5B%22message%22%5D")
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return list(data.get("result") or [])
    except Exception:  # noqa: BLE001 — транспорт не вправе уронить воркер
        return []


def own_texts(updates: List[Dict[str, Any]]) -> Tuple[List[str], Optional[int]]:
    """Тексты из чата владельца + максимальный update_id ВСЕХ прочитанных.

    Курсор двигают все update (иначе чужой мусор перечитывался бы вечно),
    но текстами становятся только сообщения из TELEGRAM_CHAT_ID.
    """
    chat = str(os.environ.get("TELEGRAM_CHAT_ID") or "")
    texts: List[str] = []
    max_id: Optional[int] = None
    for update in updates:
        try:
            max_id = max(max_id or 0, int(update.get("update_id")))
        except (TypeError, ValueError):
            continue
        message = update.get("message") or {}
        if str((message.get("chat") or {}).get("id")) != chat:
            continue
        text = message.get("text")
        if text:
            texts.append(str(text))
    return texts, max_id


def apply_one(client: WriteClient, row: Dict[str, Any]) -> Dict[str, Any]:
    """Одно одобренное действие → кабинет, той же машиной статусов, что Э1.

    Строка УЖЕ в журнале со своим previous_state и red_line — путь Э1
    (insert → send → mark) повторяется с середины: claim возвращает её в
    planned, дальше mark_sent → mutate → mark_action.
    """
    action_id = str(row["action_id"])
    base = {"action_id": action_id, "object_id": row.get("object_id"),
            "kind": row.get("action_kind")}
    if not approval_db.claim_for_apply(action_id):
        # Строку забрал другой контур (TTL, второй воркер) — не наша.
        return {**base, "result": "conflicted"}

    try:
        service, method, params = writer_apply.to_api_call(row)
    except Exception as exc:  # noqa: BLE001 — тело не собралось, запрос не уходил
        writer_db.mark_action(action_id, "failed",
                              {"error": f"{type(exc).__name__}: {exc}"[:400]})
        return {**base, "result": "failed", "error": str(exc)[:200]}

    if not client.is_write_allowed():
        # Репетиция строку не ест: claim откатывается, действие остаётся в
        # очереди — боевой запуск доберёт его по той же памяти решений.
        approval_db.release_claim(action_id)
        return {**base, "result": "dry_run", "call": [service, method]}

    sent = False
    try:
        writer_db.mark_sent(action_id)
        sent = True
        response = client.mutate(service, method, params)
        errors = writer_apply._element_errors(method, response, 0)  # noqa: SLF001
        status = "rejected" if errors else "applied"
        marked = writer_db.mark_action(action_id, status, response)
        return {**base, "result": status if marked else "conflicted",
                "errors": errors or None}
    except Exception as exc:  # noqa: BLE001
        # Граница — по факту отправки (та же, что в apply_actions): после
        # mutate любой разбор, упавший не «точно не ушло», — неизвестный
        # исход, и переприменять нельзя.
        outcome_unknown = (not isinstance(exc, DirectWriteError)
                           or is_outcome_unknown(exc))
        if sent and outcome_unknown:
            writer_db.mark_unknown_outcome(
                action_id, f"{type(exc).__name__}: {exc}"[:400])
            return {**base, "result": "unknown_outcome", "error": str(exc)[:200]}
        writer_db.mark_action(action_id, "failed",
                              {"error": f"{type(exc).__name__}: {exc}"[:400]})
        return {**base, "result": "failed", "error": str(exc)[:200]}


def run(dry_run: bool) -> Dict[str, Any]:
    approval_db.ensure_schema()

    expired = approval_db.expire_pending(approval.PENDING_TTL_HOURS)
    pending = approval_db.load_pending()

    offset = approval_db.get_offset()
    updates = fetch_updates(offset)
    texts, max_id = own_texts(updates)
    decisions = approval.parse_decisions(texts)

    # Курсор двигается ДО применения: решение, упавшее на применении,
    # закрыто статусом в журнале, а не повтором тех же сообщений — повтор
    # apply уже применённого создал бы в кабинете второй объект.
    if max_id is not None:
        approval_db.set_offset(max_id)

    report: Dict[str, Any] = {"expired": expired, "pending": len(pending),
                              "texts": len(texts), "decisions": len(decisions),
                              "applied": [], "vetoed": [], "dry_run": dry_run}
    if not pending or not decisions:
        return report

    by_code = {approval.short_code(str(r["action_id"])): r for r in pending}
    verdicts = approval.resolve_decisions(decisions, by_code)

    clients: Dict[str, WriteClient] = {}
    for code, approved in verdicts.items():
        row = by_code[code]
        key = str(row["idempotency_key"])
        if not approved:
            marked = approval_db.mark_vetoed(str(row["action_id"]))
            approval_db.record_decision(key, str(row["action_id"]),
                                        approval_db.DECISION_VETOED)
            report["vetoed"].append({"code": code, "marked": marked})
            continue
        login = str(row.get("account") or "")
        if login not in clients:
            clients[login] = WriteClient(login, sandbox=False, dry_run=dry_run)
        outcome = apply_one(clients[login], row)
        approval_db.record_decision(key, str(row["action_id"]),
                                    approval_db.DECISION_APPROVED)
        report["applied"].append({"code": code, **outcome})
    return report


def _summary(report: Dict[str, Any]) -> str:
    lines = ["Апрув-контур:"]
    applied = [a for a in report.get("applied", [])
               if a.get("result") == "applied"]
    troubled = [a for a in report.get("applied", [])
                if a.get("result") not in ("applied", "dry_run")]
    if applied:
        lines.append(f"применено {len(applied)}: "
                     + ", ".join(str(a.get("object_id")) for a in applied))
    if report.get("vetoed"):
        lines.append(f"отклонено по слову: {len(report['vetoed'])}")
    if report.get("expired"):
        lines.append(f"истёк срок (молчание = нет): {len(report['expired'])}")
    if troubled:
        lines.append("проблемы: " + "; ".join(
            f"{a.get('object_id')}: {a.get('result')}" for a in troubled))
    return "\n".join(lines)


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    try:
        with writer_db.run_lock("agent_writer"):
            report = run(dry_run)
    except writer_db.RunLockBusy:
        # Э1 или сторож пишут прямо сейчас. Решения человека лежат в чате,
        # курсор не сдвинут — следующий крон доберёт всё без потерь.
        _print({"verdict": "SKIPPED", "reason": "run_lock_busy"})
        return 0

    _print({"verdict": "OK", **report})
    # Уведомление только когда произошло хоть что-то: тишина каждые 20
    # минут превратила бы канал в шум, из-за которого не видно запросов.
    if report.get("applied") or report.get("vetoed") or report.get("expired"):
        _print({"verdict": "NOTIFY", **notify.send(_summary(report))})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
