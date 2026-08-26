# -*- coding: utf-8 -*-
"""
probe_retargeting_lever.py — есть ли под рычаг «аудитории и ретаргетинг» ДАННЫЕ.

Этап 2 конвейера экспериментов (docs/AGENT-EXPERIMENT-PIPELINE.md) начинается
с корректировок на сегменты ретаргетинга: тот же механизм bidmodifiers, что уже
работает у пола, возраста и устройств. Но механизм записи бесполезен, если
считать корректировку не по чему: сегментные срезы агента приходят из
CUSTOM_REPORT, а справочник полей ретаргетингового измерения не обещает.

Поэтому до единой строки кода рычага — три вопроса, и на каждый отвечает
кабинет, а не догадка:

  1. ЕСТЬ ЛИ ЧТО КОРРЕКТИРОВАТЬ: сколько в кабинетах условий ретаргетинга и
     аудиторий, и привязаны ли они к группам (audiencetargets).
  2. ЕСТЬ ЛИ ЗАМЕР: принимает ли Reports API поля, по которым эффективность
     сегмента вообще можно посчитать (CriterionType / Criterion / CriterionId
     и соседи). Нет замера — нет и корректировки: агент не двигает ставку по
     срезу, которого не видит.
  3. ПРИНИМАЕТ ЛИ ЗАПИСЬ: форму RetargetingAdjustments у bidmodifiers.add.
     Проверяется по ЗАВЕДОМО НЕСУЩЕСТВУЮЩЕМУ идентификатору — ошибка уровня
     элемента подтверждает форму, ничего не меняя в кабинете.

Запуск: python probe_retargeting_lever.py [--account=<логин>]
ENV (из .env): DIRECT_TOKEN, DIRECT_CLIENTS_JSON

Токен в вывод не печатается. Все операции read-only, кроме add по
несуществующему объекту, который по построению не может ничего создать.
"""

import json
import os
import sys
from datetime import date, timedelta
from typing import Any, Dict, List, Tuple

import requests
from dotenv import load_dotenv

load_dotenv()

PROD = "https://api.direct.yandex.com/json/v5"
REPORTS_URL = "https://api.direct.yandex.com/json/v5/reports"

_RIGHTS_CODES = {53, 54, 152, 513}
NONEXISTENT_ID = 999_999_999

# Кандидаты в измерение «сегмент ретаргетинга/аудитория» для CUSTOM_REPORT.
# Ни одно из них в sync/agent/segments.py::SEGMENT_FIELDS не заведено, поэтому
# каждое спрашивается отдельным запросом: отказ по 8000 на одном поле не должен
# уносить с собой остальные.
REPORT_CANDIDATES = [
    "CriterionType",
    "Criterion",
    "CriterionId",
    "TargetingCategory",
    "MatchType",
    "AdGroupName",
]


def classify(status: int, body: Dict[str, Any]) -> str:
    """Вердикт по ответу. Ошибка уровня ЭЛЕМЕНТА значит, что форма верна."""
    if status == 403:
        return "NO_ACCESS"
    error = body.get("error") or {}
    code = error.get("error_code")
    if code in _RIGHTS_CODES:
        return "NO_ACCESS"
    if code is not None:
        return f"REJECTED[{code}]"
    result = body.get("result") or {}
    if result:
        return "OK"
    return "UNKNOWN"


def _accounts() -> List[str]:
    raw = (os.environ.get("DIRECT_CLIENTS_JSON") or "").strip()
    if raw:
        return [str(item["login"]) for item in json.loads(raw)
                if isinstance(item, dict) and str(item.get("login", "")).strip()]
    return [os.environ["DIRECT_CLIENT_LOGIN"]]


def _goals(login: str) -> List[str]:
    raw = (os.environ.get("DIRECT_CLIENTS_JSON") or "").strip()
    for item in json.loads(raw or "[]"):
        if isinstance(item, dict) and str(item.get("login", "")) == login:
            return [str(g) for g in (item.get("goal_ids") or [])]
    return []


def _call(login: str, service: str, method: str,
          params: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    resp = requests.post(
        f"{PROD}/{service}",
        data=json.dumps({"method": method, "params": params},
                        ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {os.environ['DIRECT_TOKEN']}",
            "Client-Login": login,
            "Accept-Language": "ru",
            "Content-Type": "application/json; charset=utf-8",
        },
        timeout=90,
    )
    resp.encoding = "utf-8"
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, {}


def probe_report_field(login: str, field: str, goals: List[str]) -> str:
    """Принимает ли CUSTOM_REPORT это поле как измерение."""
    date_to = (date.today() - timedelta(days=1)).isoformat()
    date_from = (date.today() - timedelta(days=8)).isoformat()
    params: Dict[str, Any] = {
        "SelectionCriteria": {"DateFrom": date_from, "DateTo": date_to},
        "FieldNames": [field, "Impressions", "Clicks", "Cost"],
        "ReportName": f"probe-{field}-{date_from}",
        "ReportType": "CUSTOM_REPORT",
        "DateRangeType": "CUSTOM_DATE",
        "Format": "TSV",
        "IncludeVAT": "YES",
    }
    if goals:
        params["Goals"] = goals
    resp = requests.post(
        REPORTS_URL,
        data=json.dumps({"params": params}, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {os.environ['DIRECT_TOKEN']}",
            "Client-Login": login,
            "Accept-Language": "ru",
            "Content-Type": "application/json; charset=utf-8",
            "processingMode": "auto",
            "returnMoneyInMicros": "false",
            "skipReportHeader": "true",
            "skipReportSummary": "true",
        },
        timeout=120,
    )
    resp.encoding = "utf-8"
    if resp.status_code in (200, 201, 202):
        return "ACCEPTED"
    try:
        body = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}"
    error = body.get("error") or {}
    detail = str(error.get("error_detail") or error.get("error_string") or "")[:120]
    return f"REJECTED[{error.get('error_code')}] {detail}"


def probe_account(login: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"account": login}

    # 1. Есть ли что корректировать.
    s, b = _call(login, "retargetinglists", "get", {
        "SelectionCriteria": {},
        "FieldNames": ["Id", "Name", "Type", "Scope"],
    })
    lists = ((b.get("result") or {}).get("RetargetingLists") or [])
    out["retargeting_lists"] = {
        "verdict": classify(s, b),
        "count": len(lists),
        "by_type": _count_by(lists, "Type"),
        "by_scope": _count_by(lists, "Scope"),
    }

    # Привязки к группам: список без привязки не влияет ни на один показ, и
    # корректировать по нему нечего — разница между «сегменты есть» и «сегменты
    # работают» здесь и живёт.
    s, b = _call(login, "audiencetargets", "get", {
        "SelectionCriteria": {},
        "FieldNames": ["Id", "AdGroupId", "CampaignId", "RetargetingListId", "State"],
        "Page": {"Limit": 1000},
    })
    targets = ((b.get("result") or {}).get("AudienceTargets") or [])
    out["audience_targets"] = {
        "verdict": classify(s, b),
        "count": len(targets),
        "campaigns": len({str(t.get("CampaignId")) for t in targets}),
        "lists_used": len({str(t.get("RetargetingListId")) for t in targets}),
        "by_state": _count_by(targets, "State"),
    }

    # 2. Уже стоящие корректировки на ретаргетинг — их формат заодно показывает,
    # на каком уровне рычаг вообще живёт в этом кабинете.
    s, b = _call(login, "bidmodifiers", "get", {
        "SelectionCriteria": {"Levels": ["CAMPAIGN", "AD_GROUP"],
                              "Types": ["RETARGETING_ADJUSTMENT"]},
        "FieldNames": ["Id", "CampaignId", "AdGroupId", "Type", "Level"],
        "RetargetingAdjustmentFieldNames": ["BidModifier", "RetargetingConditionId"],
    })
    mods = ((b.get("result") or {}).get("BidModifiers") or [])
    out["existing_adjustments"] = {
        "verdict": classify(s, b),
        "count": len(mods),
        "by_level": _count_by(mods, "Level"),
        "sample": mods[:3],
    }

    # 3. Принимает ли запись форму. Идентификаторы заведомо несуществующие.
    s, b = _call(login, "bidmodifiers", "add", {
        "BidModifiers": [{
            "CampaignId": NONEXISTENT_ID,
            "RetargetingAdjustments": [
                {"RetargetingConditionId": NONEXISTENT_ID, "BidModifier": 110}],
        }],
    })
    out["add_form_campaign_level"] = {
        "verdict": classify(s, b),
        "raw": json.dumps(b, ensure_ascii=False)[:300],
    }

    s, b = _call(login, "bidmodifiers", "add", {
        "BidModifiers": [{
            "AdGroupId": NONEXISTENT_ID,
            "RetargetingAdjustments": [
                {"RetargetingConditionId": NONEXISTENT_ID, "BidModifier": 110}],
        }],
    })
    out["add_form_adgroup_level"] = {
        "verdict": classify(s, b),
        "raw": json.dumps(b, ensure_ascii=False)[:300],
    }

    # 4. Есть ли замер.
    goals = _goals(login)
    out["report_fields"] = {f: probe_report_field(login, f, goals)
                            for f in REPORT_CANDIDATES}
    return out


def _count_by(rows: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    acc: Dict[str, int] = {}
    for r in rows:
        acc[str(r.get(key))] = acc.get(str(r.get(key)), 0) + 1
    return acc


def main() -> int:
    only = ""
    for arg in sys.argv[1:]:
        if arg.startswith("--account="):
            only = arg.split("=", 1)[1]
    accounts = [a for a in _accounts() if not only or a == only]
    report = [probe_account(login) for login in accounts]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
