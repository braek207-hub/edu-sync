# -*- coding: utf-8 -*-
"""
sync/lime_direct.py — синк кабинета Яндекс Директ для LIME → lime_direct_stats.

Независимый модуль: свой токен/логин Директа, отдельный workflow
(.github/workflows/sync-lime-direct.yml). Не зависит от main.py и sync-lime.yml.

Тянет:
  - Reports API CUSTOM_REPORT (per date+campaign):
      Impressions, Clicks, Cost (с НДС),
      AvgEffectiveBid, AvgTrafficVolume, AvgImpressionPosition, AvgClickPosition,
      BounceRate (%), AvgPageviews,
      Conversions по целям (LSC) — из config/lime_direct_goals.json.
  - Campaigns.get (снапшот): DailyBudget, BiddingStrategy, PackageBiddingStrategy
    → weekly_budget (в т.ч. APP / товарные / пакетные стратегии).

Запуск:  python -m sync.lime_direct

ENV:
    DATABASE_URL
    LIME_DIRECT_TOKEN
    LIME_DIRECT_CLIENT_LOGIN
    LIME_DIRECT_DAYS_BACK  (default 7)
"""

import os
import io
import csv
import json
import hashlib
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
import psycopg2
import psycopg2.extras

REPORTS_URL = "https://api.direct.yandex.com/json/v5/reports"
CAMPAIGNS_URL = "https://api.direct.yandex.com/json/v5/campaigns"
STRATEGIES_URL = "https://api.direct.yandex.com/json/v5/strategies"

REPORT_FIELDS = [
    "Date",
    "CampaignId",
    "CampaignName",
    "Impressions",
    "Clicks",
    "Cost",
    "AvgEffectiveBid",
    "AvgTrafficVolume",
    "AvgImpressionPosition",
    "AvgClickPosition",
    "BounceRate",
    "AvgPageviews",
]

GOALS_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "lime_direct_goals.json")


def _token() -> str:
    t = os.environ.get("LIME_DIRECT_TOKEN", "").strip()
    if not t:
        raise RuntimeError("LIME_DIRECT_TOKEN не задан")
    return t


def _client_login() -> str:
    login = os.environ.get("LIME_DIRECT_CLIENT_LOGIN", "").strip()
    if not login:
        raise RuntimeError("LIME_DIRECT_CLIENT_LOGIN не задан")
    return login


def _pg_url() -> str:
    return os.environ["DATABASE_URL"].split("?")[0]


def _parse_goals() -> List[str]:
    if not os.path.exists(GOALS_CONFIG_PATH):
        return []
    with open(GOALS_CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    goal_ids = []
    for g in cfg.get("goals", []):
        gid = g.get("id")
        if gid is None:
            continue
        goal_ids.append(str(int(gid)))
    return sorted(list(set(goal_ids)))


def _report_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_token()}",
        "Client-Login": _client_login(),
        "Accept-Language": "en",
        "processingMode": "auto",
        "returnMoneyInMicros": "false",
        "skipReportHeader": "true",
        "skipColumnHeader": "false",
        "skipReportSummary": "true",
    }


def _json_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_token()}",
        "Client-Login": _client_login(),
        "Accept-Language": "en",
        "Content-Type": "application/json; charset=utf-8",
    }


def _num(v: Any) -> float:
    if v is None:
        return 0.0
    s = str(v).strip().replace(",", ".")
    if s in ("", "--"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _int(v: Any) -> int:
    return int(round(_num(v)))


def _micros_to_float(x: Any) -> Optional[float]:
    if x is None or x == "":
        return None
    try:
        return float(x) / 1_000_000.0
    except (ValueError, TypeError):
        return None


def _weekly_from_details(details: Dict[str, Any]) -> Optional[float]:
    weekly = _micros_to_float(details.get("WeeklySpendLimit"))
    if weekly is not None:
        return weekly
    cpb = details.get("CustomPeriodBudget")
    if isinstance(cpb, dict):
        return _micros_to_float(cpb.get("SpendLimit"))
    return None


def _extract_strategy_budget(strategy_block: Any) -> Dict[str, Optional[float]]:
    out: Dict[str, Optional[float]] = {"weekly_budget": None, "target_cpa": None}
    if not isinstance(strategy_block, dict):
        return out

    best_weekly: Optional[float] = None
    best_cpa: Optional[float] = None

    for k, v in strategy_block.items():
        if k in ("BiddingStrategyType", "PlacementTypes"):
            continue
        if not isinstance(v, dict):
            continue
        weekly = _weekly_from_details(v)
        if weekly is not None and best_weekly is None:
            best_weekly = weekly
        cpa = v.get("AverageCpa")
        if cpa is None:
            cpa = v.get("Cpa")
        cpa_f = _micros_to_float(cpa)
        if cpa_f is not None and best_cpa is None:
            best_cpa = cpa_f

    out["weekly_budget"] = best_weekly
    out["target_cpa"] = best_cpa
    return out


def _pick_weekly(*values: Optional[float]) -> Optional[float]:
    for v in values:
        if v is not None and v > 0:
            return v
    for v in values:
        if v is not None:
            return v
    return None


def _report_sig(fields: List[str], goal_ids: List[str]) -> str:
    src = json.dumps(
        {"fields": fields, "goals": goal_ids, "attr": ["LSC"]},
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.md5(src).hexdigest()[:8]


MAX_GOALS_PER_REPORT = 10


def _parse_row_conversions(row: Dict[str, Any], goal_ids: List[str]) -> Dict[str, int]:
    """Читает конверсии по фактическим именам колонок TSV (с запасом по префиксу)."""
    out: Dict[str, int] = {}
    for gid in goal_ids:
        exact = f"Conversions_{gid}_LSC"
        val = 0
        if exact in row:
            val = _int(row.get(exact))
        else:
            prefix = f"Conversions_{gid}_"
            for col, raw in row.items():
                if col and col.startswith(prefix):
                    val = _int(raw)
                    break
        out[str(gid)] = val
    return out


def _tsv_dict_reader(text: str) -> csv.DictReader:
    text = text.lstrip("\ufeff")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    while lines and "CampaignId" not in lines[0]:
        lines.pop(0)
    return csv.DictReader(io.StringIO("\n".join(lines)), delimiter="\t")


def _fetch_report_chunk(
    date_from: str,
    date_to: str,
    goal_ids: List[str],
    *,
    include_metrics: bool,
) -> List[Dict[str, Any]]:
    fields = list(REPORT_FIELDS) if include_metrics else ["Date", "CampaignId", "CampaignName"]
    if goal_ids:
        fields.append("Conversions")

    params: Dict[str, Any] = {
        "SelectionCriteria": {"DateFrom": date_from, "DateTo": date_to},
        "FieldNames": fields,
        "ReportName": f"lime_direct_{date_from}_{date_to}_{_report_sig(fields, goal_ids)}",
        "ReportType": "CUSTOM_REPORT",
        "DateRangeType": "CUSTOM_DATE",
        "Format": "TSV",
        "IncludeVAT": "YES",
        "IncludeDiscount": "NO",
    }
    if goal_ids:
        params["Goals"] = [int(g) for g in goal_ids]
        params["AttributionModels"] = ["LSC"]

    body = {"params": params}
    payload = json.dumps(body).encode("utf-8")

    for _ in range(8):
        r = requests.post(REPORTS_URL, data=payload, headers=_report_headers())
        r.encoding = "utf-8"
        if r.status_code == 200:
            break
        if r.status_code in (201, 202):
            retry_in = int(r.headers.get("retryIn", "30"))
            print(f"  [lime_direct] отчёт формируется, ждём {retry_in}с...")
            time.sleep(retry_in)
            continue
        raise RuntimeError(f"Reports API {r.status_code}: {r.text[:300]}")
    else:
        raise RuntimeError("Reports API: превышено число попыток")

    rows: List[Dict[str, Any]] = []
    reader = _tsv_dict_reader(r.text)
    fieldnames = reader.fieldnames or []
    conv_headers = [h for h in fieldnames if h and h.startswith("Conversions_")]
    if goal_ids and not conv_headers:
        print(f"  [lime_direct] WARN: в TSV нет колонок Conversions_*, заголовки: {fieldnames[:25]}")
    elif goal_ids and conv_headers:
        print(f"  [lime_direct] колонки конверсий: {conv_headers[:5]}{'...' if len(conv_headers) > 5 else ''}")

    for row in reader:
        cid = str(row.get("CampaignId", "")).strip()
        if not cid or cid == "--":
            continue
        item: Dict[str, Any] = {
            "date": str(row.get("Date", "")).strip(),
            "campaign_id": cid,
            "campaign_name": str(row.get("CampaignName", "")).strip(),
        }
        if include_metrics:
            item.update({
                "impressions": _int(row.get("Impressions")),
                "clicks": _int(row.get("Clicks")),
                "cost": round(_num(row.get("Cost")), 2),
                "avg_effective_bid": round(_num(row.get("AvgEffectiveBid")), 2),
                "avg_traffic_volume": _num(row.get("AvgTrafficVolume")),
                "avg_impression_position": _num(row.get("AvgImpressionPosition")),
                "avg_click_position": _num(row.get("AvgClickPosition")),
                "bounce_rate": _num(row.get("BounceRate")),
                "avg_pageviews": _num(row.get("AvgPageviews")),
            })
        item["conversions"] = _parse_row_conversions(row, goal_ids) if goal_ids else {}
        rows.append(item)
    return rows


def _fetch_report(
    date_from: str,
    date_to: str,
    goal_ids: List[str],
) -> List[Dict[str, Any]]:
    if not goal_ids:
        return _fetch_report_chunk(date_from, date_to, [], include_metrics=True)
    if len(goal_ids) <= MAX_GOALS_PER_REPORT:
        return _fetch_report_chunk(date_from, date_to, goal_ids, include_metrics=True)

    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for i, chunk in enumerate(_chunked(goal_ids, MAX_GOALS_PER_REPORT)):
        # Полный набор полей на каждом батче — иначе API отдаёт только строки с конверсиями по целям батча.
        partial = _fetch_report_chunk(
            date_from, date_to, chunk, include_metrics=True
        )
        print(f"  [lime_direct] батч целей {i + 1}: {len(chunk)} целей, {len(partial)} строк")
        for row in partial:
            key = (row["date"], row["campaign_id"])
            if key not in merged:
                merged[key] = row
            else:
                merged[key]["conversions"].update(row.get("conversions", {}))
    for row in merged.values():
        conv = row.setdefault("conversions", {})
        for gid in goal_ids:
            conv.setdefault(str(gid), 0)
    return list(merged.values())


def _chunked(seq: List[str], n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _fetch_package_strategies(strategy_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    if not strategy_ids:
        return out

    base_fields = ["Id", "Name", "Type", "StatusArchived", "AttributionModel", "CounterIds", "PriorityGoals"]
    strat_fields = {
        "StrategyMaximumClicksFieldNames": ["WeeklySpendLimit", "BidCeiling", "CustomPeriodBudget", "BudgetType"],
        "StrategyMaximumConversionRateFieldNames": ["WeeklySpendLimit", "BidCeiling", "GoalId", "CustomPeriodBudget", "BudgetType"],
        "StrategyAverageCpcFieldNames": ["AverageCpc", "WeeklySpendLimit", "CustomPeriodBudget", "BudgetType"],
        "StrategyAverageCpaFieldNames": ["AverageCpa", "GoalId", "WeeklySpendLimit", "BidCeiling", "ExplorationBudget", "CustomPeriodBudget", "BudgetType"],
        "StrategyPayForConversionFieldNames": ["Cpa", "GoalId", "WeeklySpendLimit", "CustomPeriodBudget", "BudgetType"],
        "StrategyAverageCrrFieldNames": ["Crr", "GoalId", "WeeklySpendLimit", "ExplorationBudget", "CustomPeriodBudget", "BudgetType"],
        "StrategyPayForConversionCrrFieldNames": ["Crr", "GoalId", "WeeklySpendLimit", "CustomPeriodBudget", "BudgetType"],
    }

    for part in _chunked([str(x) for x in strategy_ids], 1000):
        body = {
            "method": "get",
            "params": {
                "SelectionCriteria": {"Ids": [int(x) for x in part]},
                "FieldNames": base_fields,
                **strat_fields,
            },
        }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        r = requests.post(STRATEGIES_URL, data=data, headers=_json_headers())
        r.encoding = "utf-8"
        if r.status_code != 200:
            raise RuntimeError(f"Strategies API {r.status_code}: {r.text[:300]}")
        js = r.json()
        if "error" in js:
            raise RuntimeError(f"Strategies API error: {json.dumps(js['error'], ensure_ascii=False)}")

        for s in js.get("result", {}).get("Strategies", []) or []:
            sid = s.get("Id")
            if sid is None:
                continue
            details = None
            for k, v in s.items():
                if k in ("Id", "Name", "Type", "StatusArchived", "AttributionModel", "CounterIds", "PriorityGoals"):
                    continue
                if isinstance(v, dict):
                    details = v
                    break
            weekly = _weekly_from_details(details) if isinstance(details, dict) else None
            cpa = None
            budget_type = None
            if isinstance(details, dict):
                cpa = _micros_to_float(details.get("AverageCpa") or details.get("Cpa"))
                budget_type = "custom_period" if isinstance(details.get("CustomPeriodBudget"), dict) else ("weekly" if details.get("WeeklySpendLimit") else None)
            out[int(sid)] = {
                "weekly_budget": weekly,
                "target_cpa": cpa,
                "name": s.get("Name"),
                "budget_type": budget_type,
            }

    return out


def _fetch_campaigns(campaign_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    info: Dict[str, Dict[str, Any]] = {}
    if not campaign_ids:
        return info

    field_names = ["Id", "Name", "Type", "Status", "State", "DailyBudget"]
    text_fields = ["BiddingStrategy", "PackageBiddingStrategy"]
    unified_fields = ["BiddingStrategy", "PackageBiddingStrategy"]
    mobile_fields = ["BiddingStrategy", "PackageBiddingStrategy"]
    package_strategy_ids: set[int] = set()
    pending: Dict[str, Dict[str, Any]] = {}

    for part in _chunked(campaign_ids, 1000):
        body = {
            "method": "get",
            "params": {
                "SelectionCriteria": {"Ids": [int(x) for x in part]},
                "FieldNames": field_names,
                "TextCampaignFieldNames": text_fields,
                "UnifiedCampaignFieldNames": unified_fields,
                "MobileAppCampaignFieldNames": mobile_fields,
            },
        }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        r = requests.post(CAMPAIGNS_URL, data=data, headers=_json_headers())
        r.encoding = "utf-8"
        if r.status_code != 200:
            raise RuntimeError(f"Campaigns API {r.status_code}: {r.text[:300]}")
        js = r.json()
        if "error" in js:
            raise RuntimeError(f"Campaigns API error: {json.dumps(js['error'], ensure_ascii=False)}")

        for c in js.get("result", {}).get("Campaigns", []) or []:
            cid = str(c.get("Id", ""))
            daily = c.get("DailyBudget") or {}
            tc = c.get("TextCampaign") or {}
            uc = c.get("UnifiedCampaign") or {}
            mc = c.get("MobileAppCampaign") or {}

            tc_bs = tc.get("BiddingStrategy") or {}
            uc_bs = uc.get("BiddingStrategy") or {}
            mc_bs = mc.get("BiddingStrategy") or {}

            tc_pkg = tc.get("PackageBiddingStrategy") or {}
            uc_pkg = uc.get("PackageBiddingStrategy") or {}
            mc_pkg = mc.get("PackageBiddingStrategy") or {}

            pkg_id = None
            for pkg in (tc_pkg, uc_pkg, mc_pkg):
                if isinstance(pkg, dict) and pkg.get("StrategyId"):
                    pkg_id = int(pkg["StrategyId"])
                    break
            if pkg_id is not None:
                package_strategy_ids.add(pkg_id)

            search = _extract_strategy_budget(tc_bs.get("Search") or uc_bs.get("Search"))
            network = _extract_strategy_budget(tc_bs.get("Network") or uc_bs.get("Network"))
            mobile = _extract_strategy_budget(mc_bs)

            pending[cid] = {
                "weekly_budget": _pick_weekly(search["weekly_budget"], network["weekly_budget"], mobile["weekly_budget"]),
                "target_cpa": _pick_weekly(search["target_cpa"], network["target_cpa"], mobile["target_cpa"]),
                "daily_budget": _micros_to_float(daily.get("Amount")),
                "status": c.get("Status"),
                "state": c.get("State"),
                "campaign_type": c.get("Type"),
                "package_strategy_id": pkg_id,
                "package_strategy_name": None,
                "budget_source": "campaign" if _pick_weekly(search["weekly_budget"], network["weekly_budget"], mobile["weekly_budget"]) else None,
                "budget_type": "weekly",
            }

    strategies = _fetch_package_strategies(sorted(package_strategy_ids))
    for cid, row in pending.items():
        pkg_id = row.get("package_strategy_id")
        if (row.get("weekly_budget") is None or row.get("weekly_budget") == 0) and pkg_id is not None:
            pkg = strategies.get(int(pkg_id), {})
            if pkg.get("weekly_budget") is not None:
                row["weekly_budget"] = pkg["weekly_budget"]
                row["budget_source"] = "package"
            if row.get("target_cpa") is None and pkg.get("target_cpa") is not None:
                row["target_cpa"] = pkg["target_cpa"]
            row["package_strategy_name"] = pkg.get("name")
            row["budget_type"] = pkg.get("budget_type") or row.get("budget_type")
        elif pkg_id is not None:
            row["budget_source"] = "package"
            row["package_strategy_name"] = strategies.get(int(pkg_id), {}).get("name")
        info[cid] = row

    return info


_UPSERT_SQL = """
    INSERT INTO lime_direct_stats
      (date, campaign_id, campaign_name, client_login,
       impressions, clicks, cost,
       avg_effective_bid, avg_traffic_volume,
       avg_impression_position, avg_click_position,
       bounce_rate, avg_pageviews,
       weekly_budget, daily_budget, target_cpa,
       conversions,
       package_strategy_id, package_strategy_name, budget_source, budget_type,
       state, status, campaign_type, updated_at)
    VALUES
      (%(date)s, %(campaign_id)s, %(campaign_name)s, %(client_login)s,
       %(impressions)s, %(clicks)s, %(cost)s,
       %(avg_effective_bid)s, %(avg_traffic_volume)s,
       %(avg_impression_position)s, %(avg_click_position)s,
       %(bounce_rate)s, %(avg_pageviews)s,
       %(weekly_budget)s, %(daily_budget)s, %(target_cpa)s,
       %(conversions)s,
       %(package_strategy_id)s, %(package_strategy_name)s, %(budget_source)s, %(budget_type)s,
       %(state)s, %(status)s, %(campaign_type)s, NOW())
    ON CONFLICT (date, campaign_id) DO UPDATE SET
       campaign_name = EXCLUDED.campaign_name,
       client_login = EXCLUDED.client_login,
       impressions = EXCLUDED.impressions,
       clicks = EXCLUDED.clicks,
       cost = EXCLUDED.cost,
       avg_effective_bid = EXCLUDED.avg_effective_bid,
       avg_traffic_volume = EXCLUDED.avg_traffic_volume,
       avg_impression_position = EXCLUDED.avg_impression_position,
       avg_click_position = EXCLUDED.avg_click_position,
       bounce_rate = EXCLUDED.bounce_rate,
       avg_pageviews = EXCLUDED.avg_pageviews,
       weekly_budget = EXCLUDED.weekly_budget,
       daily_budget = EXCLUDED.daily_budget,
       target_cpa = EXCLUDED.target_cpa,
       conversions = EXCLUDED.conversions,
       package_strategy_id = EXCLUDED.package_strategy_id,
       package_strategy_name = EXCLUDED.package_strategy_name,
       budget_source = EXCLUDED.budget_source,
       budget_type = EXCLUDED.budget_type,
       state = EXCLUDED.state,
       status = EXCLUDED.status,
       campaign_type = EXCLUDED.campaign_type,
       updated_at = NOW()
"""


def _upsert(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    with psycopg2.connect(_pg_url()) as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, _UPSERT_SQL, rows, page_size=500)
        conn.commit()
    return len(rows)


def sync_lime_direct(days_back: int = 7) -> int:
    today = date.today()
    date_from = (today - timedelta(days=days_back)).isoformat()
    date_to = today.isoformat()
    client_login = _client_login()
    goal_ids = _parse_goals()

    print(f"[lime_direct] отчёт {date_from} — {date_to} ({client_login})")
    if goal_ids:
        print(f"[lime_direct] цели LSC: {', '.join(goal_ids)}")
    report_rows = _fetch_report(date_from, date_to, goal_ids)
    print(f"[lime_direct] получено {len(report_rows)} строк отчёта")
    if goal_ids and report_rows:
        conv_sum = sum(
            sum((r.get("conversions") or {}).values())
            for r in report_rows
        )
        keys = len((report_rows[0].get("conversions") or {}))
        print(f"[lime_direct] конверсии: {conv_sum:.0f} суммарно, {keys} целей в строке")
    if not report_rows:
        return 0

    campaign_ids = sorted({r["campaign_id"] for r in report_rows})
    campaigns = _fetch_campaigns(campaign_ids)
    print(f"[lime_direct] стратегии/бюджеты по {len(campaigns)} кампаниям")

    merged: List[Dict[str, Any]] = []
    for r in report_rows:
        c = campaigns.get(r["campaign_id"], {})
        merged.append({
            **r,
            "client_login": client_login,
            "weekly_budget": c.get("weekly_budget"),
            "daily_budget": c.get("daily_budget"),
            "target_cpa": c.get("target_cpa"),
            "state": c.get("state"),
            "status": c.get("status"),
            "campaign_type": c.get("campaign_type"),
            "conversions": psycopg2.extras.Json(r.get("conversions", {})),
            "package_strategy_id": c.get("package_strategy_id"),
            "package_strategy_name": c.get("package_strategy_name"),
            "budget_source": c.get("budget_source"),
            "budget_type": c.get("budget_type"),
        })

    n = _upsert(merged)
    print(f"[lime_direct] upsert {n} строк в lime_direct_stats")
    return n


if __name__ == "__main__":
    sync_lime_direct(days_back=int(os.environ.get("LIME_DIRECT_DAYS_BACK", "7")))
