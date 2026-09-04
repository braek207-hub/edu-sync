# -*- coding: utf-8 -*-
"""
sync/agent_alerts.py — почасовой сторож внутридневных тревог.

Что он делает: спрашивает Reports API про сегодня и вчера, прогоняет два
правила (sync/agent/alerts.py), отсеивает то, про что уже говорили сегодня, и
шлёт остаток в Telegram. В кабинет он не пишет и в базу пишет только отметку
«про это сказано».

Почему не из витрин: они наливаются раз в сутки, и почасовой сторож поверх
них двадцать три раза посмотрел бы на те же вчерашние числа. Что отчёт за
текущий день живой — замер probe_intraday_spend 04.09.2026.

Почему не круглые сутки: до полудня доля дня мала и у части кампаний ещё
законно ноль. Окно 12:00–20:00 МСК взято по тому же замеру — в 12:13 расход
имели ВСЕ 84 активные кампании четырёх кабинетов.

Молчание при отсутствии событий — это работа, а не сбой. Сторож, который
шлёт «всё хорошо» восемь раз в день, учит человека не читать сообщения бота.

Запуск:
    python -m sync.agent_alerts
    python -m sync.agent_alerts --dry-run    # посчитать и напечатать, не слать
ENV: DIRECT_TOKEN, DIRECT_CLIENTS_JSON, DATABASE_URL,
     TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from sync.agent import alerts as rules
from sync.agent import alerts_db, blackbox, notify
from sync.direct import _direct_clients, _fetch_report

MSK = timezone(timedelta(hours=3))

# Часы МСК, в которые сторож имеет право говорить. Раньше 12 у части кампаний
# законный ноль (замер: в 12:13 нулей уже не было ни у одной), позже 20 —
# ночь, и человек всё равно ничего не сделает до утра.
HOUR_FROM, HOUR_TO = 12, 20


def _by_campaign(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        cid = str(row.get("campaign_id") or "")
        if not cid:
            continue
        slot = out.setdefault(cid, {"name": row.get("campaign_name") or "",
                                    "cost": 0.0})
        slot["cost"] += float(row.get("cost") or 0.0)
    return out


def collect(today: str, yesterday: str) -> Dict[str, Any]:
    """Снимок расхода по кабинетам. Кабинет, который не ответил, попадает в
    failed и НЕ участвует в межкабинетном контроле: пустой снимок неотличим
    от нулевого расхода, и молчащий API выглядел бы обвалом.
    """
    accounts: Dict[str, Dict[str, Any]] = {}
    failed: List[Dict[str, str]] = []
    for client in _direct_clients():
        login = client["login"]
        goals = client.get("goal_ids") or []
        try:
            today_map = _by_campaign(_fetch_report(login, today, today, goals))
            yday_map = _by_campaign(_fetch_report(login, yesterday, yesterday, goals))
        except Exception as exc:  # noqa: BLE001 — отказ кабинета не роняет сторожа
            failed.append({"login": login,
                           "error": f"{type(exc).__name__}: {exc}"[:200]})
            continue
        today_cost = sum(r["cost"] for r in today_map.values())
        yday_cost = sum(r["cost"] for r in yday_map.values())
        accounts[login] = {
            "today": today_map, "yesterday": yday_map,
            "today_cost": today_cost, "yesterday_cost": yday_cost,
            "share": (today_cost / yday_cost) if yday_cost else None,
        }
    return {"accounts": accounts, "failed": failed}


def evaluate(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    accounts = snapshot.get("accounts") or {}
    found: List[Dict[str, Any]] = []
    for login, row in accounts.items():
        found.extend(rules.stopped_campaigns(login, row["today"], row["yesterday"]))
    found.extend(rules.collapsed_accounts(
        {login: {"share": row["share"], "today_cost": row["today_cost"],
                 "yesterday_cost": row["yesterday_cost"]}
         for login, row in accounts.items()}))
    # Обвал кабинета — причина, молчащие кампании — следствие: если по кабинету
    # сработало и то и другое, первым человек должен увидеть кабинет.
    found.sort(key=lambda a: 0 if a["rule"] == rules.RULE_ACCOUNT_COLLAPSE else 1)
    return found


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    now = datetime.now(MSK)
    today = now.date().isoformat()
    yesterday = (now.date() - timedelta(days=1)).isoformat()

    out: Dict[str, Any] = {"hour_msk": now.hour, "today": today,
                           "dry_run": dry_run}

    if not (HOUR_FROM <= now.hour <= HOUR_TO) and not dry_run:
        out["verdict"] = "OUT_OF_WINDOW"
        print(json.dumps(out, ensure_ascii=False))
        return 0

    snapshot = collect(today, yesterday)
    found = evaluate(snapshot)
    out["failed_accounts"] = snapshot["failed"]
    out["found"] = len(found)

    keys = [rules.alert_key(a, today) for a in found]
    if not dry_run:
        alerts_db.ensure_schema()
    seen = alerts_db.already_notified(keys) if not dry_run else set()
    fresh = [(a, k) for a, k in zip(found, keys) if k not in seen]
    out["fresh"] = len(fresh)

    if not fresh:
        out["verdict"] = "QUIET"
    else:
        text = rules.summary([a for a, _ in fresh], now.hour)
        out["text"] = text
        if dry_run:
            out["verdict"] = "DRY_RUN"
        else:
            sent = notify.send(text)
            out["notify"] = sent
            # Отметка ТОЛЬКО после успешной отправки: не дошло — скажем в
            # следующий час, а отметка вперёд отправки означала бы молчание
            # про событие навсегда.
            if sent.get("sent"):
                alerts_db.mark_notified([a for a, _ in fresh],
                                        [k for _, k in fresh], today)
            out["verdict"] = "ALERTED" if sent.get("sent") else "SEND_FAILED"

    if not dry_run:
        out["blackbox"] = blackbox.save_run(
            blackbox.new_run_id(), stage="alerts", mode=blackbox.MODE_COMPUTE,
            report={**out, "alerts": found})
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    # Отказавший кабинет — красный ран: письмо GitHub, иначе сторож умрёт молча.
    return 1 if snapshot["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
