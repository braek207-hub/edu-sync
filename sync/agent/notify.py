# -*- coding: utf-8 -*-
"""Уведомления человеку о боевых тактах.

Сегодня единственный сигнал о прогоне — красный шаг воркфлоу; всё остальное
живёт в чёрном ящике и требует, чтобы человек сам туда зашёл. Молчание —
тоже сигнал, поэтому шлём сообщение и когда «применено 0»: иначе тишина
неотличима от того, что крон не отработал вовсе.

Транспорт — Bot API через urllib (без внешних зависимостей). Отказ сети не
роняет прогон: send() ничего не бросает, а результат возвращается полем и
уходит рядом с чёрным ящиком в тот же отчёт.

Текст — ЧИСТЫЙ plain text, без parse_mode. Имена кампаний в отчётах несут
"_" и "*" как обычные символы; под Markdown-разметкой Telegram они рвут
сообщение или меняют его смысл, а экранировать их дороже, чем не включать
разметку вовсе.
"""
import json
import os
import urllib.request
from typing import Any, Dict

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
# Лимит Bot API на текст сообщения — 4096 символов; отчёт обрезается, а не
# отбрасывается: усечённая сводка лучше отсутствующей.
MAX_TEXT_LEN = 4000
TOP_REJECTS_SHOWN = 4


def _post(url: str, data: bytes) -> None:
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json; charset=utf-8"})
    urllib.request.urlopen(req, timeout=10).read()


def send(text: str) -> Dict[str, Any]:
    """Шлёт text в чат. Никогда не бросает — результат всегда в поле."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return {"sent": False, "reason": "not_configured"}
    try:
        _post(TELEGRAM_API.format(token=token),
              json.dumps({"chat_id": chat, "text": text[:MAX_TEXT_LEN]},
                        ensure_ascii=False).encode("utf-8"))
        return {"sent": True, "reason": None}
    except Exception as exc:  # noqa: BLE001 — транспорт не вправе уронить прогон
        return {"sent": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}


def _top_rejects(rejects: Dict[str, int]) -> str:
    ordered = sorted((rejects or {}).items(), key=lambda kv: -kv[1])[:TOP_REJECTS_SHOWN]
    return ", ".join(f"{k} {v}" for k, v in ordered) or "—"


def e1_summary(report: Dict[str, Any], dry_run: bool) -> str:
    """Сводка такта записи Э1. Ключи — из report.accounts[i]:

    result: {applied, failed, dry_run, skipped, deferred, rejected,
             units_low, conflicted, unknown_outcome} — БЕЗ "stale". В
    репетиции ничего фактически не применяется, поэтому счётчик того, что
    применилось бы, — result["dry_run"], а не result["applied"].
    """
    mode = "репетиция" if dry_run else "БОЕВАЯ ЗАПИСЬ"
    lines = [f"Агент Э1 · {mode} · {report.get('verdict')}"]
    for acc in report.get("accounts") or []:
        res = acc.get("result") or {}
        taken = (acc.get("lanes") or {}).get("taken") or {}
        applied = res.get("dry_run", 0) if dry_run else res.get("applied", 0)
        lanes_text = ", ".join(f"{k} {v}" for k, v in taken.items()) or "—"
        lines.append(
            f"{acc.get('account')}: план {acc.get('planned')}, применено {applied}, "
            f"сбой {res.get('failed', 0)}, неясный исход {res.get('unknown_outcome', 0)}; "
            f"полосы {lanes_text}; отказы {_top_rejects(acc.get('rejects') or {})}")
    if report.get("failed_accounts"):
        lines.append("Кабинеты с ошибкой: " +
                     ", ".join(a.get("account", "?") for a in report["failed_accounts"]))
    return "\n".join(lines)


def watchdog_summary(out: Dict[str, Any]) -> str:
    """Сводка такта отката (сторож). Верхний уровень отчёта несёт только
    alarms/under_watch/needs_manual_rollback; rolled_back/breached/
    rollback_failed/closed_verdicts/needs_review живут ПО АККАУНТАМ в
    out["accounts"][i] — breached там уже int (счётчик), не список.
    """
    lines = [f"Сторож · {out.get('verdict')}"]
    if out.get("alarms"):
        lines.append("ТРЕВОГИ: " + "; ".join(out["alarms"]))
    for acc in out.get("accounts") or []:
        closed = sum((acc.get("closed_verdicts") or {}).values())
        lines.append(
            f"{acc.get('account')}: откатов {acc.get('rolled_back', 0)}, "
            f"пробоев {acc.get('breached', 0)}, "
            f"неоткаченных {acc.get('rollback_failed', 0)}, "
            f"закрыто наблюдений {closed}, к разбору {acc.get('needs_review', 0)}")
    lines.append(f"под наблюдением {out.get('under_watch', 0)}, "
                 f"неоткатываемых вручную {out.get('needs_manual_rollback', 0)}")
    return "\n".join(lines)
