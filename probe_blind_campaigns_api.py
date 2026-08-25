# -*- coding: utf-8 -*-
"""
probe_blind_campaigns_api.py — почему кампании, тратящие деньги СЕЙЧАС, не
попали в витрину настроек.

Замер 25.08.2026 (probe_blind_spend_window, run 32866947540): за окно решений
(28 дней) слепы 17 кампаний из 87 на 3,45 млн ₽ — 14,4 % расхода. Крупные из
них по имени опознаются как Мастер кампаний («/ МК /»), и это ожидаемая зона:
МК в API кампаний не отдаётся. Но в списке есть и обычные августовские
кампании онлайн-школы, которые обязаны были попасть в витрину: синк
sync/edu_direct_settings.py ходит по всем логинам ежедневно (последний —
25.08 03:04 UTC), а витрина их не содержит.

Гипотез две, и обе проверяются одним запросом:

1. Кампании не отдаёт сам API — тогда виноват фильтр состояний в
   _list_campaigns_for_login (ON/OFF/SUSPENDED/ENDED, без ARCHIVED и
   CONVERTED) или тип кампании, недоступный методу campaigns.get.
2. Кампании API отдаёт — тогда дефект в самом синке: они выпадают на этапе
   чтения настроек, молча, и «слепая зона» на деле шире, чем Мастер кампаний.

Спрашиваем API по конкретным Id, БЕЗ фильтра состояний, и печатаем, что он
про них знает: Type, State, Status, кабинет. Разбор по логинам — Id кампании
принадлежит одному кабинету, а токен-заголовок Client-Login у каждого свой.

Скрипт read-only: ничего не пишет ни в базу, ни в кабинет.
"""

import json
from typing import Dict, List

import psycopg2.extras
import requests

from sync.db import get_connection
from sync.direct import _direct_clients
from sync import edu_direct_settings as S


def _blind_ids(conn) -> List[Dict[str, object]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT f.campaign_id::text AS campaign_id,
                   MAX(f.campaign_name) AS campaign_name,
                   ROUND(SUM(f.cost)) AS cost
            FROM edu_agent_facts f
            LEFT JOIN edu_campaign_settings s
                   ON s.campaign_id::text = f.campaign_id::text
            WHERE f.fact_date >= CURRENT_DATE - 28
              AND s.campaign_id IS NULL
              AND f.campaign_id::text ~ '^[0-9]+$'
            GROUP BY f.campaign_id
            ORDER BY SUM(f.cost) DESC
        """)
        return [dict(r) for r in cur.fetchall()]


def _ask_api(login: str, ids: List[str]) -> Dict[str, Dict[str, object]]:
    """campaigns.get по конкретным Id без фильтра состояний."""
    S._CURRENT_LOGIN = login
    body = {
        "method": "get",
        "params": {
            "SelectionCriteria": {"Ids": [int(i) for i in ids]},
            "FieldNames": ["Id", "Name", "Type", "State", "Status"],
            "Page": {"Limit": 10000},
        },
    }
    r = requests.post(S.CAMPAIGNS_URL, headers=S._json_headers(),
                      data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                      timeout=120)
    r.raise_for_status()
    payload = r.json()
    if payload.get("error"):
        return {"__error__": payload["error"]}
    items = (payload.get("result") or {}).get("Campaigns") or []
    return {str(c["Id"]): {"name": c.get("Name"), "type": c.get("Type"),
                           "state": c.get("State"), "status": c.get("Status")}
            for c in items}


def main() -> int:
    with get_connection() as conn:
        blind = _blind_ids(conn)

    ids = [row["campaign_id"] for row in blind]
    found: Dict[str, Dict[str, object]] = {}
    errors: Dict[str, object] = {}
    for client in _direct_clients():
        login = client["login"]
        try:
            answer = _ask_api(login, ids)
        except Exception as exc:  # noqa: BLE001
            errors[login] = f"{type(exc).__name__}: {exc}"[:300]
            continue
        if "__error__" in answer:
            errors[login] = answer["__error__"]
            continue
        for campaign_id, info in answer.items():
            found[campaign_id] = {**info, "login": login}

    out = []
    for row in blind:
        campaign_id = row["campaign_id"]
        info = found.get(campaign_id)
        out.append({
            "campaign_id": campaign_id,
            "campaign_name": row["campaign_name"],
            "cost_28d": float(row["cost"]),
            "in_api": info is not None,
            **({"api": info} if info else {}),
        })

    visible = [r for r in out if r["in_api"]]
    print(json.dumps({
        "blind_campaigns": len(out),
        "cost_blind_28d": round(sum(r["cost_28d"] for r in out), 2),
        # Главное число замера: сколько слепых денег API на самом деле ОТДАЁТ.
        # Всё, что здесь больше нуля, — дефект синка, а не свойство Мастера
        # кампаний, и чинится кодом, а не сессией в интерфейсе.
        "visible_in_api": len(visible),
        "cost_visible_in_api": round(sum(r["cost_28d"] for r in visible), 2),
        "api_errors": errors,
        "campaigns": out,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
