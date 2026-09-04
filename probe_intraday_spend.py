# -*- coding: utf-8 -*-
"""
probe_intraday_spend.py — видно ли расход ТЕКУЩЕГО дня и с каким отставанием.

Зачем. План второго пилота обещает алерты аномалий «в течение часа». Витрины
EDU наливаются раз в сутки (sync.yml — 04:50 МСК, последний синк 05:40 МСК),
поэтому почасовой сторож поверх витрин физически нечего проверять: он двадцать
три раза посмотрит на те же вчерашние числа. Единственный источник, который
может знать про сегодня, — Reports API Директа.

Что проверяется фактом, а не рассуждением:
  1. отдаёт ли отчёт строки за СЕГОДНЯ (CUSTOM_DATE, DateFrom=DateTo=сегодня
     по МСК) и по скольким кампаниям;
  2. насколько сегодняшний расход к моменту прогона отличается от вчерашнего
     ЗА ВЕСЬ день — это верхняя оценка «доли дня», которую алерт увидит;
  3. сколько кампаний тратили вчера, но сегодня к этому часу молчат — это и
     есть кандидаты в правило «кампания встала». Число ложных срабатываний
     такого правила видно прямо здесь: если молчащих десятки, правило без
     учёта расписания показов не годится.

Отдельно печатается час прогона по МСК: без него «сегодня потрачено 12 %»
ничего не значит.

Скрипт read-only: в базу не пишет, в кабинет не пишет, только stdout.

Запуск: python probe_intraday_spend.py
ENV: DIRECT_TOKEN, DIRECT_CLIENTS_JSON
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from sync.direct import _direct_clients, _fetch_report

MSK = timezone(timedelta(hours=3))

# Расход ниже этого порога за день — не «кампания работала», а хвост открутки
# по остаткам. Кандидатов в «встала» такие кампании давать не должны.
MIN_YESTERDAY_COST = 300.0


def _by_campaign(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        cid = str(row.get("campaign_id") or "")
        if not cid:
            continue
        slot = out.setdefault(cid, {"name": row.get("campaign_name") or "",
                                    "cost": 0.0, "clicks": 0})
        slot["cost"] += float(row.get("cost") or 0.0)
        slot["clicks"] += int(row.get("clicks") or 0)
    return out


def main() -> int:
    now = datetime.now(MSK)
    today = now.date().isoformat()
    yesterday = (now.date() - timedelta(days=1)).isoformat()

    out: Dict[str, Any] = {
        "probe_at_msk": now.strftime("%Y-%m-%d %H:%M"),
        "hour_msk": now.hour,
        "today": today,
        "yesterday": yesterday,
        "accounts": [],
    }

    for client in _direct_clients():
        login = client["login"]
        goals = client.get("goal_ids") or []
        account: Dict[str, Any] = {"login": login}
        try:
            today_rows = _fetch_report(login, today, today, goals)
            yday_rows = _fetch_report(login, yesterday, yesterday, goals)
        except Exception as exc:  # noqa: BLE001 — проба обязана назвать отказ
            account["error"] = f"{type(exc).__name__}: {exc}"[:300]
            out["accounts"].append(account)
            continue

        today_map = _by_campaign(today_rows)
        yday_map = _by_campaign(yday_rows)
        today_cost = sum(c["cost"] for c in today_map.values())
        yday_cost = sum(c["cost"] for c in yday_map.values())

        # Вчера тратили заметно, сегодня к этому часу — ноль.
        silent = [
            {"campaign_id": cid, "name": row["name"],
             "yesterday_cost": round(row["cost"], 2)}
            for cid, row in yday_map.items()
            if row["cost"] >= MIN_YESTERDAY_COST
            and float(today_map.get(cid, {}).get("cost") or 0.0) == 0.0
        ]
        silent.sort(key=lambda r: -r["yesterday_cost"])

        active_yday = sum(1 for r in yday_map.values()
                          if r["cost"] >= MIN_YESTERDAY_COST)
        account.update({
            "today_rows": len(today_rows),
            "today_campaigns_with_cost": sum(1 for r in today_map.values()
                                             if r["cost"] > 0),
            "today_cost": round(today_cost, 2),
            "yesterday_campaigns_active": active_yday,
            "yesterday_cost": round(yday_cost, 2),
            # Доля дня, видимая к этому часу. Если она около нуля при непустом
            # вчера — отчёт за сегодня приходит с отставанием, и алерт на нём
            # строить нельзя.
            "today_share_of_yesterday": (round(today_cost / yday_cost, 4)
                                         if yday_cost else None),
            "silent_today": len(silent),
            "silent_share_of_active": (round(len(silent) / active_yday, 4)
                                       if active_yday else None),
            "silent_top": silent[:10],
        })
        out["accounts"].append(account)

    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
