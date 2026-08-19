# -*- coding: utf-8 -*-
"""
sync/agent_e1.py — прогон Э1a: применение вычисленных настроек.

Порядок: гейт данных → план → свежий факт из API → diff → рельсы → заповедник →
риск-бюджет → применение → сторож красных линий.

По умолчанию ПЕСОЧНИЦА и DRY-RUN. Боевая запись требует двух явных флагов:
--prod и --apply. Это не перестраховка: единственный необратимый шаг здесь —
касание живого кабинета, и он должен быть намеренным.

Запуск:
    python -m sync.agent_e1                    # песочница, dry-run
    python -m sync.agent_e1 --prod             # боевой кабинет, dry-run
    python -m sync.agent_e1 --prod --apply     # боевая запись
ENV: DATABASE_URL, DIRECT_TOKEN, DIRECT_CLIENTS_JSON

Два отклонения от исходного плана задачи (см. task-9-report.md):

1. Список кампаний кабинета берётся через campaigns.get ЭТОГО кабинета,
   а не как срез справочника расходов по всем кабинетам сразу. Справочник
   расходов (edu_agent_facts) копит кампании ВСЕХ клиентов в одной таблице;
   без пересечения с собственным списком кампаний агент слал бы
   bidmodifiers.get по чужим Id — гарантированная ошибка "объект не найден"
   и лишние Units на чужой кабинет.

2. Демографические корректировки нормализуются построчно: одна запись
   DemographicsAdjustment в ответе API может нести Gender И Age ОДНОВРЕМЕННО
   (ставка на пересечение сегментов). Diff сопоставляет план с фактом по паре
   (Type, key) — если такую запись свернуть в один ключ, вторая половина
   потеряется и diff предложит add там, где нужен set, создав дубль
   корректировки в кабинете.
"""

import json
import os
import sys
from datetime import date, timedelta
from typing import Any, Dict, List

from sync.agent import db as agent_db
from sync.agent.writer import db as writer_db
from sync.agent.writer.apply import apply_actions
from sync.agent.writer.client import WriteClient
from sync.agent.writer.diff import diff_modifiers
from sync.agent.writer.guardrails import cap_actions, check_action, check_holdout
from sync.agent.writer.plan import desired_bid_modifiers
from sync.agent.writer.risk import action_risk, fit_into_budget, week_start
from sync.agent.writer.rollback import red_line_for

DEFAULT_WEEKLY_RISK_RUB = 50_000.0
CAMPAIGN_PAGE_LIMIT = 1000


def _clients() -> List[Dict[str, Any]]:
    raw = (os.environ.get("DIRECT_CLIENTS_JSON") or "").strip()
    out: List[Dict[str, Any]] = []
    if raw:
        for item in json.loads(raw):
            if isinstance(item, dict) and str(item.get("login", "")).strip():
                out.append({"login": item["login"]})
    return out


def fetch_campaign_ids(client: WriteClient) -> List[int]:
    """Id всех кампаний ОДНОГО кабинета (форма — sync/agent/segments.py::fetch_campaign_ids).

    Постранично: Page.Limit/Offset, остановка когда страница короче лимита.
    """
    out: List[int] = []
    offset = 0
    while True:
        result = client.get("campaigns", {
            "SelectionCriteria": {},
            "FieldNames": ["Id"],
            "Page": {"Limit": CAMPAIGN_PAGE_LIMIT, "Offset": offset},
        })
        items = result.get("Campaigns") or []
        out += [int(c["Id"]) for c in items]
        if len(items) < CAMPAIGN_PAGE_LIMIT:
            break
        offset += CAMPAIGN_PAGE_LIMIT
    return out


def own_campaign_ids(client: WriteClient, daily_cost_by_campaign: Dict[str, float]) -> List[str]:
    """Кампании ЭТОГО кабинета, пересечённые со справочником расходов.

    daily_cost_by_campaign построен по ВСЕМ кабинетам сразу — без пересечения
    с собственным списком кампаний агент опрашивал бы чужие Id чужим логином.
    """
    own = {str(i) for i in fetch_campaign_ids(client)}
    return sorted(own & set(daily_cost_by_campaign.keys()))


def _normalize_actual(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Одна запись bidmodifiers.get → 0..N нормализованных actual-записей.

    Демографическая корректировка может нести Gender И Age одновременно —
    такая запись раскладывается в ДВЕ отдельные normalized-записи с разными
    ключами (но одним Id: обе указывают на один физический объект в Директе).
    Без этого diff терял бы вторую половину и предлагал add вместо set.
    """
    out: List[Dict[str, Any]] = []
    mobile = item.get("MobileAdjustment") or {}
    demo = item.get("DemographicsAdjustment") or {}
    regional = item.get("RegionalAdjustment") or {}

    if mobile:
        out.append({"Id": item["Id"], "Type": "MOBILE_ADJUSTMENT",
                    "key": "mobile", "percent": int(mobile.get("BidModifier") or 0)})
    if demo:
        percent = int(demo.get("BidModifier") or 0)
        gender = demo.get("Gender")
        age = demo.get("Age")
        if gender:
            out.append({"Id": item["Id"], "Type": "DEMOGRAPHICS_ADJUSTMENT",
                        "key": gender, "percent": percent})
        if age:
            out.append({"Id": item["Id"], "Type": "DEMOGRAPHICS_ADJUSTMENT",
                        "key": age, "percent": percent})
    if regional:
        out.append({"Id": item["Id"], "Type": "REGIONAL_ADJUSTMENT",
                    "key": str(regional.get("RegionId") or ""),
                    "percent": int(regional.get("BidModifier") or 0)})
    return out


def _actual_modifiers(client: WriteClient, campaign_id: str) -> List[Dict[str, Any]]:
    """Свежее состояние корректировок кампании: между прогонами кабинет могли
    править руками, поэтому читаем заново на каждом прогоне, а не берём из журнала.

    Levels обязателен и лежит ВНУТРИ SelectionCriteria (probe задачи 1, факт
    подтверждён прогонами 32217815538 и др.) — без него запрос отвергается
    ошибкой 8000 «Отсутствует обязательный параметр Levels».
    """
    result = client.get("bidmodifiers", {
        "SelectionCriteria": {"CampaignIds": [int(campaign_id)], "Levels": ["CAMPAIGN"]},
        "FieldNames": ["Id", "CampaignId", "Type"],
        "MobileAdjustmentFieldNames": ["BidModifier"],
        "DemographicsAdjustmentFieldNames": ["BidModifier", "Gender", "Age"],
        "RegionalAdjustmentFieldNames": ["BidModifier", "RegionId"],
    })
    out: List[Dict[str, Any]] = []
    for item in result.get("BidModifiers") or []:
        out += _normalize_actual(item)
    return out


def main() -> int:
    sandbox = "--prod" not in sys.argv
    dry_run = "--apply" not in sys.argv
    today = date.today().isoformat()

    writer_db.ensure_writer_tables()

    computed = agent_db.load_latest_computed_settings()
    desired = desired_bid_modifiers(computed)
    if not desired:
        print(json.dumps({"verdict": "NOTHING_TO_DO", "reason": "нет значимых корректировок"},
                         ensure_ascii=False, indent=2))
        return 0

    clients = _clients()
    if not clients:
        print(json.dumps({"verdict": "NOTHING_TO_DO", "reason": "нет кабинетов в DIRECT_CLIENTS_JSON"},
                         ensure_ascii=False, indent=2))
        return 0

    holdout_ids = set(agent_db.load_holdout_ids())
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    daily_cost = agent_db.load_daily_cost_by_campaign(cutoff, today)
    baseline_cpa = agent_db.load_baseline_cpa(cutoff, today)
    wk = week_start(today)

    for client_info in clients:
        client = WriteClient(client_info["login"], sandbox=sandbox, dry_run=dry_run)

        # Рулинг 1: кампании только этого кабинета, не всего справочника расходов.
        campaign_ids = own_campaign_ids(client, daily_cost)

        planned: List[Dict[str, Any]] = []
        blocked: List[Dict[str, Any]] = []

        for campaign_id in campaign_ids:
            actual = _actual_modifiers(client, campaign_id)
            for action in diff_modifiers(desired, actual, campaign_id):
                ok, reason = check_action(action)
                if not ok:
                    blocked.append({**action, "blocked_reason": reason})
                    continue
                planned.append({**action, "account": client_info["login"]})

        allowed, in_holdout = check_holdout(planned, holdout_ids)
        blocked += [{**a, "blocked_reason": "заповедник"} for a in in_holdout]

        risks = {a["idempotency_key"]: action_risk(a, daily_cost) for a in allowed}
        # Бюджет читается заново для каждого кабинета: он общий на весь прогон,
        # а не на кабинет, и предыдущий клиент этого же прогона мог его уже
        # частично занять (spent_risk читает applied_at из журнала, куда
        # apply_actions уже успел записать применённые действия).
        remaining = writer_db.risk_limit(wk, DEFAULT_WEEKLY_RISK_RUB) - writer_db.spent_risk(wk)
        fits, deferred = fit_into_budget(allowed, risks, remaining)
        fits, over_cap = cap_actions(fits)

        # Красная линия ставится ВМЕСТЕ с действием: у каждого применённого
        # изменения заранее известно, при каком исходе оно считается провалом.
        prepared = []
        for a in fits:
            baseline = {"cpa": baseline_cpa.get(str(a["object_id"]), 0.0)}
            prepared.append({
                **a,
                "risk_rub": risks[a["idempotency_key"]],
                "red_line": red_line_for(a, baseline),
            })
        report = apply_actions(client, prepared, writer_db)

        print(json.dumps({
            "account": client_info["login"],
            "sandbox": sandbox,
            "dry_run": dry_run,
            "own_campaigns": len(campaign_ids),
            "desired": len(desired),
            "planned": len(planned),
            "blocked": len(blocked),
            "deferred_by_risk": len(deferred),
            "deferred_by_cap": len(over_cap),
            "remaining_risk_rub": round(remaining, 2),
            "result": {k: v for k, v in report.items() if k != "details"},
            "units_left": client.units_left,
        }, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
