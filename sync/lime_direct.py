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
      Conversions по целям (LSC) — цели из config/lime_direct_goals.json.
  - Campaigns.get (снапшот): DailyBudget, BiddingStrategy, PackageBiddingStrategy
    → weekly_budget (в т.ч. APP / товарные / пакетные стратегии).
  - Настройки кампаний (per campaign, lime_campaign_settings):
      стратегия, аудитория, таргетинг, корректировки, офферный таргетинг.

Запуск:  python -m sync.lime_direct

ENV:
    DATABASE_URL
    LIME_DIRECT_TOKEN
    LIME_DIRECT_CLIENT_LOGIN
    LIME_DIRECT_DAYS_BACK  (default 7)

Цели НЕ в env: список берётся из config/lime_direct_goals.json (см. _parse_goals).
"""

import os
import io
import csv
import json
import hashlib
import time
import traceback
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
import psycopg2
import psycopg2.extras

REPORTS_URL = "https://api.direct.yandex.com/json/v5/reports"
CAMPAIGNS_URL = "https://api.direct.yandex.com/json/v5/campaigns"
CAMPAIGNS_V501_URL = "https://api.direct.yandex.com/json/v501/campaigns"
STRATEGIES_URL = "https://api.direct.yandex.com/json/v5/strategies"
ADGROUPS_URL = "https://api.direct.yandex.com/json/v5/adgroups"
BIDMODIFIERS_URL = "https://api.direct.yandex.com/json/v5/bidmodifiers"
AUDIENCETARGETS_URL = "https://api.direct.yandex.com/json/v5/audiencetargets"
RETARGETINGLISTS_URL = "https://api.direct.yandex.com/json/v5/retargetinglists"
KEYWORDS_URL = "https://api.direct.yandex.com/json/v5/keywords"
FEEDS_URL = "https://api.direct.yandex.com/json/v5/feeds"
GOALS_URL = "https://api.direct.yandex.com/json/v5/goals"
DICTIONARIES_URL = "https://api.direct.yandex.com/json/v5/dictionaries"
SMARTADTARGETS_URL = "https://api.direct.yandex.com/json/v5/smartadtargets"

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

GOALS_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "config", "lime_direct_goals.json"
)

# Жёсткий лимит Reports API: «Array Goals cannot contain more than 10 elements».
# Целей 12 → отчёт запрашивается порциями, результаты склеиваются по (date, campaign_id).
GOALS_PER_REPORT = 10

_OS_LABELS = {"ANDROID": "Android", "IOS": "iOS"}

_GENDER_RU = {"GENDER_MALE": "Мужчины", "GENDER_FEMALE": "Женщины"}
_AGE_RU = {
    "AGE_0_17": "0–17",
    "AGE_18_24": "18–24",
    "AGE_25_34": "25–34",
    "AGE_35_44": "35–44",
    "AGE_45_54": "45–54",
    "AGE_55": "55+",
}

# Русские названия регионов (API getGeoRegions часто отдаёт EN для округов).
GEO_REGION_RU: Dict[int, str] = {
    225: "Россия",
    73: "Республика Башкортостан",
    102444: "Дальневосточный федеральный округ",
    102445: "Сибирский федеральный округ",
    102446: "Северо-Кавказский федеральный округ",
    102447: "Северо-Западный федеральный округ",
    102448: "Уральский федеральный округ",
    102449: "Приволжский федеральный округ",
    102450: "Центральный федеральный округ",
    102451: "Южный федеральный округ",
}

GEO_REGION_EN_TO_RU: Dict[str, str] = {
    "Russia": "Россия",
    "Far Eastern Federal District": "Дальневосточный федеральный округ",
    "Republic of Bashkortostan": "Республика Башкортостан",
    "North Caucasian Federal District": "Северо-Кавказский федеральный округ",
    "Siberian Federal District": "Сибирский федеральный округ",
    "Northwestern Federal District": "Северо-Западный федеральный округ",
    "Ural Federal District": "Уральский федеральный округ",
    "Volga Federal District": "Приволжский федеральный округ",
    "Central Federal District": "Центральный федеральный округ",
    "Southern Federal District": "Южный федеральный округ",
}


_CRR_STRATEGY_FIELD_NAMES: Dict[str, List[str]] = {
    "StrategyAverageCrrFieldNames": [
        "Crr", "GoalId", "WeeklySpendLimit", "ExplorationBudget", "CustomPeriodBudget", "BudgetType",
    ],
    "StrategyPayForConversionCrrFieldNames": [
        "Crr", "GoalId", "WeeklySpendLimit", "CustomPeriodBudget", "BudgetType",
    ],
}

_STRATEGY_DETAIL_FIELD_NAMES: Dict[str, List[str]] = {
    "StrategyMaximumClicksFieldNames": ["WeeklySpendLimit", "BidCeiling", "CustomPeriodBudget", "BudgetType"],
    "StrategyMaximumConversionRateFieldNames": ["WeeklySpendLimit", "BidCeiling", "GoalId", "CustomPeriodBudget", "BudgetType"],
    "StrategyAverageCpcFieldNames": ["AverageCpc", "WeeklySpendLimit", "CustomPeriodBudget", "BudgetType"],
    "StrategyAverageCpaFieldNames": [
        "AverageCpa", "GoalId", "WeeklySpendLimit", "BidCeiling", "ExplorationBudget", "CustomPeriodBudget", "BudgetType",
    ],
    "StrategyPayForConversionFieldNames": ["Cpa", "GoalId", "WeeklySpendLimit", "CustomPeriodBudget", "BudgetType"],
    **_CRR_STRATEGY_FIELD_NAMES,
}


def _geo_region_label(rid: int, api_name: str) -> str:
    abs_rid = abs(int(rid))
    return GEO_REGION_RU.get(abs_rid) or GEO_REGION_EN_TO_RU.get(api_name, api_name)


def _format_demo_detail(demo: Dict[str, Any]) -> str:
    parts: List[str] = []
    gender = demo.get("Gender")
    if gender and str(gender) not in ("None", ""):
        parts.append(_GENDER_RU.get(str(gender), str(gender)))
    age = demo.get("Age")
    if age and str(age) not in ("None", ""):
        parts.append(_AGE_RU.get(str(age), str(age)))
    return " · ".join(parts)


def _infer_bid_kind(strategy_type: Optional[str]) -> str:
    if strategy_type and "CRR" in str(strategy_type).upper():
        return "drr"
    return "cpa"


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


def _parse_goals() -> Tuple[List[str], Dict[str, str]]:
    """ID целей для Reports API — из config/lime_direct_goals.json.

    Файл зеркалит config/lime-direct-goals.json дашборда: 12 целей на 4 ступени
    (install/cart/checkout/purchase) с разбивкой по платформам.

    Читаем файл, а не env: плоский env-формат на шесть ключей не выражает 12 целей
    и не имеет ступени install вовсе. На этой замене синк потерял установки
    2026-06-24 (коммит 47dc2b6) — conversions приходил как {}, а в дашборде молча
    умерли все 12 колонок целей.

    Возвращает (список id для Reports API, map goal_id → ступень).
    """
    with open(GOALS_CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    goal_ids: List[str] = []
    id_to_step: Dict[str, str] = {}
    for g in cfg.get("goals") or []:
        gid = str(int(g["id"]))
        if gid in id_to_step:
            continue
        goal_ids.append(gid)
        id_to_step[gid] = str(g["step"])
    return goal_ids, id_to_step


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


def _crr_to_drr_percent(crr: Any) -> Optional[float]:
    """ДРР: Crr в API — доля × 1_000_000 (150000 = 15%) или целый процент (15)."""
    if crr is None:
        return None
    try:
        val = int(crr)
    except (TypeError, ValueError):
        return None
    if val <= 0:
        return None
    if val <= 100:
        return round(float(val), 2)
    frac = val / 1_000_000.0
    return round(frac * 100, 2)


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


def _pick_conversion_columns(
    header: List[str],
    goal_ids: List[str],
    id_to_step: Dict[str, str],
) -> Dict[str, str]:
    """Сопоставить колонки конверсий отчёта целям — по ПРЕФИКСУ, не по точному имени.

    Имя колонки — `Conversions_<goalId>_<модель атрибуции>`, и модель ставит API,
    а не мы: на запрос AttributionModels=["LSC"] Директ отдаёт колонки `_LSCCD`
    (Last Significant Click Cross-Device). Захардкоженный суффикс `_LSC` перестал
    совпадать — все 12 целей молча стали нулями, отчёт при этом приходил успешно.

    Поиск по префиксу переживает смену модели. Несовпадение остаётся громким:
    молчание в этом месте уже стоило месяца пустых колонок в дашборде.
    """
    cols: Dict[str, str] = {}
    missing: List[str] = []
    for gid in goal_ids:
        if gid not in id_to_step:
            continue
        prefix = f"Conversions_{gid}_"
        match = next((c for c in header if c.startswith(prefix)), None)
        if match:
            cols[match] = gid
        else:
            missing.append(gid)
    if missing:
        print(
            f"  [lime_direct] WARN: в отчёте нет колонок для целей {missing[:5]}"
            f" (всего {len(missing)}); пришли: {[c for c in header if c.startswith('Conv')][:5]}"
        )
    return cols


def _merge_report_chunks(chunks: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Склеить порции отчёта по (date, campaign_id).

    Объёмные поля (клики/расход/позиции) одинаковы во всех порциях — период и набор
    кампаний те же, меняется только список запрошенных целей. Поэтому берём строку
    из первой порции, где она встретилась, и ДОБАВЛЯЕМ к ней conversions остальных.
    Суммировать объёмные поля нельзя — это задвоило бы расход.
    """
    merged: Dict[tuple, Dict[str, Any]] = {}
    for chunk in chunks:
        for row in chunk:
            key = (row.get("date"), row.get("campaign_id"))
            base = merged.get(key)
            if base is None:
                merged[key] = dict(row)
                continue
            base.setdefault("conversions", {}).update(row.get("conversions") or {})
    return list(merged.values())


def _fetch_report(
    date_from: str,
    date_to: str,
    goal_ids: List[str],
    id_to_key: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Отчёт Реports API. Цели запрашиваются порциями: API принимает максимум 10.

    Лимит («Array Goals cannot contain more than 10 elements») и стал причиной того,
    что 2026-06-24 список целей урезали до шести ключей в env — вместе с ним потеряли
    ступень install. Порционный запрос закрывает лимит без потери целей.
    """
    if len(goal_ids) > GOALS_PER_REPORT:
        chunks = [
            _fetch_report_chunk(date_from, date_to, part, id_to_key)
            for part in _chunked(goal_ids, GOALS_PER_REPORT)
        ]
        return _merge_report_chunks(chunks)
    return _fetch_report_chunk(date_from, date_to, goal_ids, id_to_key)


def _fetch_report_chunk(
    date_from: str,
    date_to: str,
    goal_ids: List[str],
    id_to_key: Dict[str, str],
) -> List[Dict[str, Any]]:
    fields = list(REPORT_FIELDS)
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
    reader = csv.DictReader(io.StringIO(r.text), delimiter="\t")
    conv_cols = _pick_conversion_columns(list(reader.fieldnames or []), goal_ids, id_to_key)

    for row in reader:
        cid = str(row.get("CampaignId", "")).strip()
        if not cid or cid == "--":
            continue
        item: Dict[str, Any] = {
            "date": str(row.get("Date", "")).strip(),
            "campaign_id": cid,
            "campaign_name": str(row.get("CampaignName", "")).strip(),
            "impressions": _int(row.get("Impressions")),
            "clicks": _int(row.get("Clicks")),
            "cost": round(_num(row.get("Cost")), 2),
            "avg_effective_bid": round(_num(row.get("AvgEffectiveBid")), 2),
            "avg_traffic_volume": _num(row.get("AvgTrafficVolume")),
            "avg_impression_position": _num(row.get("AvgImpressionPosition")),
            "avg_click_position": _num(row.get("AvgClickPosition")),
            "bounce_rate": _num(row.get("BounceRate")),
            "avg_pageviews": _num(row.get("AvgPageviews")),
            "conversions": {},
        }
        for col, gid in conv_cols.items():
            val = _int(row.get(col))
            if val:
                item["conversions"][gid] = val
        rows.append(item)
    return rows


def _chunked(seq: List[str], n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _fetch_package_strategies(strategy_ids: List[int]) -> Dict[int, Dict[str, Optional[float]]]:
    out: Dict[int, Dict[str, Optional[float]]] = {}
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
            if isinstance(details, dict):
                cpa = _micros_to_float(details.get("AverageCpa") or details.get("Cpa"))
            out[int(sid)] = {"weekly_budget": weekly, "target_cpa": cpa}

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
            }

    strategies = _fetch_package_strategies(sorted(package_strategy_ids))
    for cid, row in pending.items():
        pkg_id = row.pop("package_strategy_id", None)
        if (row.get("weekly_budget") is None or row.get("weekly_budget") == 0) and pkg_id is not None:
            pkg = strategies.get(int(pkg_id), {})
            if pkg.get("weekly_budget") is not None:
                row["weekly_budget"] = pkg["weekly_budget"]
            if row.get("target_cpa") is None and pkg.get("target_cpa") is not None:
                row["target_cpa"] = pkg["target_cpa"]
        info[cid] = row

    return info


def _direct_post(url: str, body: Dict[str, Any]) -> Dict[str, Any]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    r = requests.post(url, data=data, headers=_json_headers())
    r.encoding = "utf-8"
    if r.status_code != 200:
        raise RuntimeError(f"Direct API {url} {r.status_code}: {r.text[:300]}")
    js = r.json()
    if "error" in js:
        raise RuntimeError(f"Direct API error: {json.dumps(js['error'], ensure_ascii=False)}")
    return js.get("result") or {}


def _paginate_items(url: str, result_key: str, body_base: Dict[str, Any], limit: int = 1000) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    offset = 0
    while True:
        body = dict(body_base)
        params = dict(body.get("params") or {})
        params["Page"] = {"Limit": limit, "Offset": offset}
        body["params"] = params
        result = _direct_post(url, body)
        chunk = result.get(result_key) or []
        if not chunk:
            break
        items.extend(chunk)
        if len(chunk) < limit:
            break
        offset += limit
        if offset >= 10000:
            break
    return items


def _goal_ids_from_block(details: Dict[str, Any]) -> List[int]:
    gid = details.get("GoalId")
    if gid is not None:
        gid_int = int(gid)
        # 13 = placeholder «приоритетные цели» в API Директа, не реальная цель.
        if gid_int == 13:
            return []
        return [gid_int]
    return []


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        items = value.get("Items")
        if isinstance(items, list):
            return items
    return []


def _priority_goal_ids_from_block(block: Dict[str, Any]) -> List[int]:
    out: List[int] = []
    for pg in _as_list(block.get("PriorityGoals")):
        if isinstance(pg, dict) and pg.get("GoalId") is not None:
            out.append(int(pg["GoalId"]))
        elif isinstance(pg, (int, float)):
            out.append(int(pg))
    return out


def _crr_from_raw_strategy_block(block: Any) -> Optional[float]:
    """Crr из сырого блока Network/Search (AverageCrr и т.п.)."""
    if not isinstance(block, dict):
        return None
    for key in (
        "AverageCrr", "PayForConversionCrr",
        "AverageCrrMultipleGoals", "PayForConversionCrrMultipleGoals",
    ):
        sub = block.get(key)
        if isinstance(sub, dict) and sub.get("Crr") is not None:
            drr = _crr_to_drr_percent(sub.get("Crr"))
            if drr is not None and drr > 0:
                return drr
    return None


def _enrich_channel_crr(ch: Dict[str, Any], raw_block: Any) -> Dict[str, Any]:
    if ch.get("targetDrr"):
        return ch
    drr = _crr_from_raw_strategy_block(raw_block)
    if drr is not None:
        ch["targetDrr"] = drr
    return ch


def _apply_crr_to_row(row: Dict[str, Any], tc_bs: Dict[str, Any], uc_bs: Dict[str, Any], mc_bs: Dict[str, Any]) -> None:
    """Дозаполняет targetDrr из сырого BiddingStrategy без полного перепарса кампании."""
    strat = row.get("strategy") or {}
    pairs = (
        ("search", tc_bs.get("Search") or uc_bs.get("Search") or mc_bs.get("Search")),
        ("network", tc_bs.get("Network") or uc_bs.get("Network") or mc_bs.get("Network")),
    )
    for ch_key, raw in pairs:
        ch = strat.get(ch_key)
        if not isinstance(ch, dict):
            continue
        bs = str(ch.get("biddingStrategyType") or "")
        if "CRR" not in bs.upper() or ch.get("targetDrr"):
            continue
        drr = _crr_from_raw_strategy_block(raw)
        if drr is not None:
            ch["targetDrr"] = drr


def _backfill_missing_crr(out: Dict[str, Dict[str, Any]], campaign_ids: List[str]) -> None:
    missing: List[str] = []
    for cid in campaign_ids:
        row = out.get(cid)
        if not row:
            continue
        strat = row.get("strategy") or {}
        for ch_key in ("network", "search"):
            ch = strat.get(ch_key)
            if not isinstance(ch, dict):
                continue
            bs = str(ch.get("biddingStrategyType") or "")
            if "CRR" in bs.upper() and not ch.get("targetDrr"):
                missing.append(cid)
                break
    if not missing:
        return

    print(f"  [lime_direct] CRR backfill для {len(missing)} кампаний (v501 campaigns.get)")
    type_fields = ["BiddingStrategy"]
    for part in _chunked(missing, 100):
        for url in (CAMPAIGNS_V501_URL, CAMPAIGNS_URL):
            body = {
                "method": "get",
                "params": {
                    "SelectionCriteria": {"Ids": [int(x) for x in part]},
                    "FieldNames": ["Id"],
                    "TextCampaignFieldNames": type_fields,
                    "UnifiedCampaignFieldNames": type_fields,
                    "MobileAppCampaignFieldNames": type_fields,
                },
            }
            try:
                result = _direct_post(url, body)
            except RuntimeError as e:
                print(f"  [lime_direct] CRR backfill {url}: {e}")
                continue
            for c in result.get("Campaigns") or []:
                cid = str(c.get("Id", ""))
                row = out.get(cid)
                if not row:
                    continue
                tc_bs = (c.get("TextCampaign") or {}).get("BiddingStrategy") or {}
                uc_bs = (c.get("UnifiedCampaign") or {}).get("BiddingStrategy") or {}
                mc_bs = (c.get("MobileAppCampaign") or {}).get("BiddingStrategy") or {}
                _apply_crr_to_row(row, tc_bs, uc_bs, mc_bs)
                if not (row.get("strategy") or {}).get("network", {}).get("targetDrr") and not (
                    row.get("strategy") or {}
                ).get("search", {}).get("targetDrr"):
                    for raw in (
                        tc_bs.get("Network"),
                        uc_bs.get("Network"),
                        tc_bs.get("Search"),
                        uc_bs.get("Search"),
                    ):
                        if isinstance(raw, dict):
                            ac = raw.get("AverageCrr") or raw.get("PayForConversionCrr")
                            if isinstance(ac, dict) and not ac.get("Crr"):
                                print(
                                    f"  [lime_direct] AverageCrr без Crr, cid={cid}, keys={list(ac.keys())}"
                                )
                                break

    still_missing = 0
    for cid in missing:
        row = out.get(cid) or {}
        strat = row.get("strategy") or {}
        for ch_key in ("network", "search"):
            ch = strat.get(ch_key)
            if isinstance(ch, dict) and "CRR" in str(ch.get("biddingStrategyType") or "").upper():
                if not ch.get("targetDrr"):
                    still_missing += 1
                    break
    if still_missing:
        print(f"  [lime_direct] WARN: Crr не получен для {still_missing} CRR-кампаний")


def _priority_goals_details_from_block(
    block: Dict[str, Any],
    *,
    is_crr_strategy: bool = False,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for pg in _as_list(block.get("PriorityGoals")):
        if isinstance(pg, dict) and pg.get("GoalId") is not None:
            gid = int(pg["GoalId"])
            if gid == 13:
                continue
            entry: Dict[str, Any] = {"goalId": gid, "bidKind": "cpa"}
            if not is_crr_strategy:
                val = pg.get("Value")
                if val is not None:
                    entry["bidValue"] = _micros_to_float(int(val))
            out.append(entry)
        elif isinstance(pg, (int, float)):
            gid = int(pg)
            if gid != 13:
                out.append({"goalId": gid, "bidKind": "cpa", "bidValue": None})
    return out


def _merge_priority_goals_details(
    blocks: List[Dict[str, Any]],
    *,
    is_crr_strategy: bool = False,
) -> List[Dict[str, Any]]:
    merged: Dict[int, Dict[str, Any]] = {}
    for block in blocks:
        for item in _priority_goals_details_from_block(block, is_crr_strategy=is_crr_strategy):
            merged[int(item["goalId"])] = item
    return list(merged.values())


def _counter_ids_from_block(block: Dict[str, Any]) -> List[int]:
    out: List[int] = []
    for counter in _as_list(block.get("CounterIds")):
        if counter is not None:
            out.append(int(counter))
    return out


def _extract_strategy_channel_full(strategy_block: Any) -> Dict[str, Any]:
    if not isinstance(strategy_block, dict):
        return {}
    out: Dict[str, Any] = {
        "biddingStrategyType": strategy_block.get("BiddingStrategyType"),
        "weeklyBudget": None,
        "targetCpa": None,
        "averageCpc": None,
        "targetDrr": None,
        "bidCeiling": None,
        "goalIds": [],
        "placementTypes": list(strategy_block.get("PlacementTypes") or []),
    }
    goal_ids: List[int] = []
    for k, v in strategy_block.items():
        if k in ("BiddingStrategyType", "PlacementTypes"):
            continue
        if not isinstance(v, dict):
            continue
        weekly = _weekly_from_details(v)
        if weekly is not None:
            out["weeklyBudget"] = weekly
        cpa = _micros_to_float(v.get("AverageCpa") or v.get("Cpa"))
        if cpa is not None:
            out["targetCpa"] = cpa
        cpc = _micros_to_float(v.get("AverageCpc"))
        if cpc is not None:
            out["averageCpc"] = cpc
        drr = _crr_to_drr_percent(v.get("Crr"))
        if drr is not None and drr > 0:
            out["targetDrr"] = drr
        ceiling = _micros_to_float(v.get("BidCeiling"))
        if ceiling is not None:
            out["bidCeiling"] = ceiling
        for gid in _goal_ids_from_block(v):
            if gid not in goal_ids:
                goal_ids.append(gid)
    out["goalIds"] = goal_ids
    return out


def _fetch_package_strategies_full(strategy_ids: List[int]) -> Tuple[Dict[int, Dict[str, Any]], List[int]]:
    out: Dict[int, Dict[str, Any]] = {}
    counter_ids: List[int] = []
    if not strategy_ids:
        return out, counter_ids

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
        result = _direct_post(STRATEGIES_URL, body)
        for s in result.get("Strategies") or []:
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
            drr = None
            goal_ids: List[int] = []
            if isinstance(details, dict):
                cpa = _micros_to_float(details.get("AverageCpa") or details.get("Cpa"))
                drr = _crr_to_drr_percent(details.get("Crr"))
                goal_ids = _goal_ids_from_block(details)
            out[int(sid)] = {
                "id": int(sid),
                "name": s.get("Name"),
                "type": s.get("Type"),
                "weeklyBudget": weekly,
                "targetCpa": cpa,
                "targetDrr": drr,
                "goalIds": goal_ids,
            }
            for counter in _counter_ids_from_block(s):
                if counter is not None:
                    counter_ids.append(counter)
    return out, counter_ids


def _fetch_campaigns_for_settings(campaign_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Полные настройки кампании из campaigns.get (+ v501 для UNIFIED)."""
    out: Dict[str, Dict[str, Any]] = {}
    if not campaign_ids:
        return out

    field_names = ["Id", "Name", "Type", "Status", "State", "DailyBudget", "NegativeKeywords"]
    type_fields = ["Settings", "BiddingStrategy", "PriorityGoals", "PackageBiddingStrategy", "CounterIds"]
    # MobileAppCampaign допускает только эти поля (без PriorityGoals/CounterIds).
    mobile_app_fields = ["Settings", "BiddingStrategy", "PackageBiddingStrategy"]
    package_ids: set[int] = set()
    counter_ids: set[int] = set()

    def _parse_campaign(c: Dict[str, Any]) -> None:
        cid = str(c.get("Id", ""))
        if not cid:
            return
        daily = c.get("DailyBudget") or {}
        tc = c.get("TextCampaign") or {}
        uc = c.get("UnifiedCampaign") or {}
        mc = c.get("MobileAppCampaign") or {}

        tc_bs = tc.get("BiddingStrategy") or {}
        uc_bs = uc.get("BiddingStrategy") or {}
        mc_bs = mc.get("BiddingStrategy") or {}

        pkg = None
        for p in (tc.get("PackageBiddingStrategy"), uc.get("PackageBiddingStrategy"), mc.get("PackageBiddingStrategy")):
            if isinstance(p, dict) and p.get("StrategyId"):
                pkg = p
                package_ids.add(int(p["StrategyId"]))
                break

        for block in (tc, uc, mc):
            for counter in _counter_ids_from_block(block):
                counter_ids.add(counter)

        settings_opts: List[str] = []
        for block in (tc, uc, mc):
            for opt in block.get("Settings") or []:
                if isinstance(opt, dict) and opt.get("Option") == "YES":
                    settings_opts.append(str(opt.get("Name", "")))

        priority_goals: List[int] = []
        for block in (tc, uc, mc):
            priority_goals.extend(_priority_goal_ids_from_block(block))

        network_ch = _extract_strategy_channel_full(tc_bs.get("Network") or uc_bs.get("Network") or mc_bs.get("Network"))
        search_ch = _extract_strategy_channel_full(tc_bs.get("Search") or uc_bs.get("Search") or mc_bs.get("Search"))
        network_ch = _enrich_channel_crr(network_ch, tc_bs.get("Network") or uc_bs.get("Network") or mc_bs.get("Network"))
        search_ch = _enrich_channel_crr(search_ch, tc_bs.get("Search") or uc_bs.get("Search") or mc_bs.get("Search"))
        bid_kind = _infer_bid_kind(
            (network_ch or {}).get("biddingStrategyType")
            or (search_ch or {}).get("biddingStrategyType")
        )
        priority_goals_details = _merge_priority_goals_details(
            [tc, uc, mc],
            is_crr_strategy=(bid_kind == "drr"),
        )

        placements: List[str] = []
        for key in (
            "SearchResults", "ProductGallery", "DynamicPlaces", "Maps",
            "SearchOrganizationList", "Network", "SearchResult",
        ):
            for bs in (tc_bs, uc_bs):
                search = bs.get("Search") or {}
                if isinstance(search, dict) and search.get(key) == "YES":
                    placements.append(key)
                network = bs.get("Network") or {}
                if isinstance(network, dict) and network.get(key) == "YES":
                    placements.append(key)

        if not search_ch and not network_ch and mc_bs:
            search_ch = _extract_strategy_channel_full(mc_bs)

        out[cid] = {
            "campaign_name": c.get("Name"),
            "meta": {
                "campaignType": c.get("Type"),
                "state": c.get("State"),
                "status": c.get("Status"),
            },
            "strategy": {
                "search": search_ch or None,
                "network": network_ch or None,
                "package": {
                    "id": int(pkg["StrategyId"]) if pkg else None,
                } if pkg else None,
                "priorityGoals": sorted(set(priority_goals)),
                "priorityGoalsDetails": priority_goals_details,
                "dailyBudget": _micros_to_float(daily.get("Amount")),
            },
            "targeting": {
                "placements": sorted(set(placements)),
                "campaignSettings": sorted(set(settings_opts)),
                "negativeKeywords": list((c.get("NegativeKeywords") or {}).get("Items") or []),
            },
        }

    for part in _chunked(campaign_ids, 1000):
        body = {
            "method": "get",
            "params": {
                "SelectionCriteria": {"Ids": [int(x) for x in part]},
                "FieldNames": field_names,
                "TextCampaignFieldNames": type_fields,
                "UnifiedCampaignFieldNames": type_fields,
                "MobileAppCampaignFieldNames": mobile_app_fields,
                "TextCampaignSearchStrategyPlacementTypesFieldNames": [
                    "SearchResults", "ProductGallery", "DynamicPlaces",
                ],
                "UnifiedCampaignSearchStrategyPlacementTypesFieldNames": [
                    "SearchResults", "ProductGallery", "DynamicPlaces", "Maps", "SearchOrganizationList",
                ],
            },
        }
        result = _direct_post(CAMPAIGNS_URL, body)
        for c in result.get("Campaigns") or []:
            _parse_campaign(c)

        unified_ids = [
            x for x in part
            if out.get(x, {}).get("meta", {}).get("campaignType") == "UNIFIED_CAMPAIGN"
        ]
        if unified_ids:
            body501 = {
                "method": "get",
                "params": {
                    "SelectionCriteria": {"Ids": [int(x) for x in unified_ids]},
                    "FieldNames": field_names,
                    "UnifiedCampaignFieldNames": type_fields,
                    "UnifiedCampaignSearchStrategyPlacementTypesFieldNames": [
                        "SearchResults", "ProductGallery", "DynamicPlaces", "Maps", "SearchOrganizationList",
                    ],
                },
            }
            try:
                result501 = _direct_post(CAMPAIGNS_V501_URL, body501)
                for c in result501.get("Campaigns") or []:
                    _parse_campaign(c)
            except RuntimeError as e:
                print(f"  [lime_direct] v501 campaigns.get: {e}")

    packages, pkg_counters = _fetch_package_strategies_full(sorted(package_ids))
    counter_ids.update(pkg_counters)
    for cid, row in out.items():
        pkg = (row.get("strategy") or {}).get("package")
        if not pkg or not pkg.get("id"):
            continue
        full = packages.get(int(pkg["id"]))
        if full:
            row["strategy"]["package"] = full
            for ch_key in ("network", "search"):
                ch = row["strategy"].get(ch_key)
                if not isinstance(ch, dict):
                    continue
                bs = str(ch.get("biddingStrategyType") or "")
                if "CRR" in bs.upper() and not ch.get("targetDrr") and full.get("targetDrr"):
                    ch["targetDrr"] = full["targetDrr"]

    missing = [cid for cid in campaign_ids if cid not in out]
    if missing:
        print(f"  [lime_direct] WARN: campaigns.get не вернул {len(missing)} кампаний, добираю")
        all_states = ["ON", "OFF", "SUSPENDED", "ENDED", "CONVERTED", "ARCHIVED"]

        def _full_params(ids: List[int]) -> Dict[str, Any]:
            return {
                "method": "get",
                "params": {
                    "SelectionCriteria": {"Ids": ids, "States": all_states},
                    "FieldNames": field_names,
                    "TextCampaignFieldNames": type_fields,
                    "UnifiedCampaignFieldNames": type_fields,
                    "MobileAppCampaignFieldNames": mobile_app_fields,
                    "TextCampaignSearchStrategyPlacementTypesFieldNames": [
                        "SearchResults", "ProductGallery", "DynamicPlaces",
                    ],
                    "UnifiedCampaignSearchStrategyPlacementTypesFieldNames": [
                        "SearchResults", "ProductGallery", "DynamicPlaces", "Maps", "SearchOrganizationList",
                    ],
                },
            }

        # 1) батч с полными type-полями (continue, не break — один сбой не роняет остальные пачки)
        for part in _chunked(missing, 100):
            try:
                result_retry = _direct_post(CAMPAIGNS_URL, _full_params([int(x) for x in part]))
                for c in result_retry.get("Campaigns") or []:
                    _parse_campaign(c)
            except RuntimeError as e:
                print(f"  [lime_direct] campaigns.get retry батч: {e}")

        # 2) кто всё ещё не добран — поштучно: сначала полные поля, иначе только общие.
        #    Общие поля (Id/Name/Type/State) не падают на отсутствии type-полей и
        #    гарантируют, что карточка получит тип/статус. Лог Type — диагностика,
        #    какие *FieldNames добавить, чтобы дотянуть полные настройки.
        for cid in [c for c in missing if c not in out]:
            try:
                r = _direct_post(CAMPAIGNS_URL, _full_params([int(cid)]))
                for c in r.get("Campaigns") or []:
                    _parse_campaign(c)
            except RuntimeError as e:
                print(f"  [lime_direct] campaigns.get id={cid} полные поля: {e}")
            if cid in out:
                continue
            try:
                r = _direct_post(CAMPAIGNS_URL, {
                    "method": "get",
                    "params": {
                        "SelectionCriteria": {"Ids": [int(cid)], "States": all_states},
                        "FieldNames": field_names,
                    },
                })
                for c in r.get("Campaigns") or []:
                    _parse_campaign(c)
                    print(f"  [lime_direct] id={cid} добран только общими полями, Type={c.get('Type')}")
            except RuntimeError as e:
                print(f"  [lime_direct] campaigns.get id={cid} общие поля: {e}")

    _backfill_missing_crr(out, campaign_ids)

    out["_counter_ids"] = sorted(counter_ids)
    return out


def _fetch_adgroups_by_campaign(campaign_ids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {cid: [] for cid in campaign_ids}
    for part in _chunked(campaign_ids, 10):
        body = {
            "method": "get",
            "params": {
                "SelectionCriteria": {"CampaignIds": [int(x) for x in part]},
                "FieldNames": [
                    "Id", "Name", "CampaignId", "Status", "Type",
                    "RegionIds", "RestrictedRegionIds", "ServingStatus",
                    "NegativeKeywords",
                ],
                "SmartAdGroupFieldNames": ["FeedId"],
                "UnifiedAdGroupFieldNames": ["OfferRetargeting"],
            },
        }
        for ag in _paginate_items(ADGROUPS_URL, "AdGroups", body):
            cid = str(ag.get("CampaignId", ""))
            if cid in out:
                feed_id = None
                smart = ag.get("SmartAdGroup") or {}
                if isinstance(smart, dict):
                    feed_id = smart.get("FeedId")
                out[cid].append({
                    "id": ag.get("Id"),
                    "name": ag.get("Name"),
                    "type": ag.get("Type"),
                    "status": ag.get("Status"),
                    "regionIds": list(ag.get("RegionIds") or []),
                    "restrictedRegionIds": list(ag.get("RestrictedRegionIds") or []),
                    "negativeKeywords": list((ag.get("NegativeKeywords") or {}).get("Items") or []),
                    "feedId": feed_id,
                    "offerRetargeting": (
                        (ag.get("UnifiedAdGroup") or {}).get("OfferRetargeting")
                        if isinstance(ag.get("UnifiedAdGroup"), dict)
                        else None
                    ),
                })
    return out


def _parse_single_bid_modifier(bm: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    btype = str(bm.get("Type", ""))
    base: Dict[str, Any] = {
        "id": bm.get("Id"),
        "type": btype,
        "level": bm.get("Level"),
        "percent": None,
        "detail": None,
        "regionId": None,
        "typeLabel": None,
    }

    def _from_adj(adj: Dict[str, Any], label: str, with_os: bool = False) -> None:
        if adj.get("BidModifier") is None:
            return
        base["percent"] = int(adj["BidModifier"])
        base["typeLabel"] = label
        if with_os:
            os_type = adj.get("OperatingSystemType")
            if os_type:
                base["typeLabel"] = f"{label} {_OS_LABELS.get(str(os_type), os_type)}"

    if btype == "MOBILE_ADJUSTMENT":
        adj = bm.get("MobileAdjustment")
        if isinstance(adj, dict):
            _from_adj(adj, "Смартфоны", True)
    elif btype == "TABLET_ADJUSTMENT":
        adj = bm.get("TabletAdjustment")
        if isinstance(adj, dict):
            _from_adj(adj, "Планшеты", True)
    elif btype == "DESKTOP_ADJUSTMENT":
        adj = bm.get("DesktopAdjustment")
        if isinstance(adj, dict):
            _from_adj(adj, "Десктоп")
    elif btype == "DESKTOP_ONLY_ADJUSTMENT":
        adj = bm.get("DesktopOnlyAdjustment")
        if isinstance(adj, dict):
            _from_adj(adj, "Только десктоп")
    elif btype == "SMART_TV_ADJUSTMENT":
        adj = bm.get("SmartTvAdjustment")
        if isinstance(adj, dict):
            _from_adj(adj, "Smart TV")
    elif btype == "SMART_AD_ADJUSTMENT":
        adj = bm.get("SmartAdAdjustment")
        if isinstance(adj, dict):
            _from_adj(adj, "Смарт-объявления")
    elif btype == "AD_GROUP_ADJUSTMENT":
        adj = bm.get("AdGroupAdjustment")
        if isinstance(adj, dict):
            _from_adj(adj, "Группа объявлений")
    elif btype == "REGIONAL_ADJUSTMENT":
        reg = bm.get("RegionalAdjustment")
        if isinstance(reg, dict):
            base["percent"] = int(reg.get("BidModifier") or 0)
            region_id = reg.get("RegionId")
            base["regionId"] = int(region_id) if region_id is not None else None
            base["detail"] = f"Регион {region_id}" if region_id is not None else None
    elif btype == "DEMOGRAPHICS_ADJUSTMENT":
        demo = bm.get("DemographicsAdjustment")
        if isinstance(demo, dict):
            base["percent"] = int(demo.get("BidModifier") or 0)
            base["detail"] = _format_demo_detail(demo) or None
    elif btype == "RETARGETING_ADJUSTMENT":
        ret = bm.get("RetargetingAdjustment")
        if isinstance(ret, dict):
            base["percent"] = int(ret.get("BidModifier") or 0)
            base["detail"] = str(ret.get("RetargetingConditionId", ""))
    else:
        for key, label, with_os in (
            ("MobileAdjustment", "Смартфоны", True),
            ("TabletAdjustment", "Планшеты", True),
            ("DesktopAdjustment", "Десктоп", False),
            ("DesktopOnlyAdjustment", "Только десктоп", False),
            ("SmartTvAdjustment", "Smart TV", False),
        ):
            adj = bm.get(key)
            if isinstance(adj, dict):
                _from_adj(adj, label, with_os)
                break

    if base["percent"] is None:
        return None
    return base


def _fetch_bidmodifiers_by_campaign(campaign_ids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {cid: [] for cid in campaign_ids}
    for part in _chunked(campaign_ids, 10):
        body = {
            "method": "get",
            "params": {
                "SelectionCriteria": {
                    "CampaignIds": [int(x) for x in part],
                    "Levels": ["CAMPAIGN", "AD_GROUP"],
                },
                "FieldNames": ["Id", "CampaignId", "AdGroupId", "Level", "Type"],
                "MobileAdjustmentFieldNames": ["BidModifier", "OperatingSystemType"],
                "TabletAdjustmentFieldNames": ["BidModifier", "OperatingSystemType"],
                "DesktopAdjustmentFieldNames": ["BidModifier"],
                "DesktopOnlyAdjustmentFieldNames": ["BidModifier"],
                "SmartTvAdjustmentFieldNames": ["BidModifier"],
                "RegionalAdjustmentFieldNames": ["RegionId", "BidModifier"],
                "DemographicsAdjustmentFieldNames": ["Gender", "Age", "BidModifier"],
                "RetargetingAdjustmentFieldNames": ["RetargetingConditionId", "BidModifier"],
                "SmartAdAdjustmentFieldNames": ["BidModifier"],
                "SerpLayoutAdjustmentFieldNames": ["SerpLayout", "BidModifier"],
                "IncomeGradeAdjustmentFieldNames": ["Grade", "BidModifier"],
                "AdGroupAdjustmentFieldNames": ["BidModifier"],
            },
        }
        for bm in _paginate_items(BIDMODIFIERS_URL, "BidModifiers", body):
            cid = str(bm.get("CampaignId", ""))
            if cid not in out:
                continue
            parsed = _parse_single_bid_modifier(bm)
            if parsed:
                out[cid].append(parsed)
    return out


def _fetch_audiencetargets_by_campaign(campaign_ids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {cid: [] for cid in campaign_ids}
    for part in _chunked(campaign_ids, 10):
        body = {
            "method": "get",
            "params": {
                "SelectionCriteria": {"CampaignIds": [int(x) for x in part]},
                "FieldNames": ["Id", "AdGroupId", "CampaignId", "RetargetingListId", "State"],
            },
        }
        for t in _paginate_items(AUDIENCETARGETS_URL, "AudienceTargets", body):
            cid = str(t.get("CampaignId", ""))
            if cid in out:
                out[cid].append({
                    "id": t.get("Id"),
                    "adGroupId": t.get("AdGroupId"),
                    "retargetingListId": t.get("RetargetingListId"),
                    "state": t.get("State"),
                })
    return out


def _fetch_retargetinglists_map(list_ids: List[int]) -> Dict[int, str]:
    if not list_ids:
        return {}
    names: Dict[int, str] = {}
    for part in _chunked([str(x) for x in list_ids], 1000):
        body = {
            "method": "get",
            "params": {
                "SelectionCriteria": {"Ids": [int(x) for x in part]},
                "FieldNames": ["Id", "Name"],
            },
        }
        for rl in _paginate_items(RETARGETINGLISTS_URL, "RetargetingLists", body):
            rid = rl.get("Id")
            if rid is not None:
                names[int(rid)] = str(rl.get("Name") or "")
    return names


def _fetch_autotargeting_by_campaign(
    campaign_ids: List[str],
    adgroups_map: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Dict[str, Any]]:
    """Сводка автотаргетинга: totalGroups из adgroups; enabled — по keywords без лишних FieldNames."""
    out: Dict[str, Dict[str, Any]] = {}
    for cid in campaign_ids:
        total = len(adgroups_map.get(cid, []))
        out[cid] = {"enabledGroups": 0, "totalGroups": total, "categories": [], "keywordsCount": 0}

    categories_by_cid: Dict[str, set] = {cid: set() for cid in campaign_ids}
    for part in _chunked(campaign_ids, 5):
        body = {
            "method": "get",
            "params": {
                "SelectionCriteria": {"CampaignIds": [int(x) for x in part]},
                "FieldNames": ["Id", "AdGroupId", "CampaignId", "Keyword", "State"],
            },
        }
        try:
            for kw in _paginate_items(KEYWORDS_URL, "Keywords", body):
                cid = str(kw.get("CampaignId", ""))
                if cid not in out:
                    continue
                keyword = str(kw.get("Keyword") or "").strip().lower()
                state = str(kw.get("State") or "ON")
                if keyword and keyword != "---autotargeting":
                    out[cid]["keywordsCount"] = int(out[cid]["keywordsCount"]) + 1
                if keyword == "---autotargeting" and state == "ON":
                    out[cid]["enabledGroups"] = int(out[cid]["enabledGroups"]) + 1
                ats = kw.get("AutotargetingSettings")
                if isinstance(ats, dict) and state == "ON":
                    out[cid]["enabledGroups"] = int(out[cid]["enabledGroups"]) + 1
                    for cat in ats.get("Categories") or []:
                        if isinstance(cat, dict) and cat.get("Selected") == "YES":
                            categories_by_cid[cid].add(str(cat.get("Category", "")))
                        elif isinstance(cat, str):
                            categories_by_cid[cid].add(cat)
        except RuntimeError as e:
            print(f"  [lime_direct] keywords/autotargeting skip: {e}")

    for cid in campaign_ids:
        out[cid]["categories"] = sorted(c for c in categories_by_cid[cid] if c)
    return out


def _fetch_feeds_map(feed_ids: List[int]) -> Dict[int, str]:
    if not feed_ids:
        return {}
    names: Dict[int, str] = {}
    for part in _chunked([str(x) for x in feed_ids], 1000):
        body = {
            "method": "get",
            "params": {
                "SelectionCriteria": {"Ids": [int(x) for x in part]},
                "FieldNames": ["Id", "Name"],
            },
        }
        for f in _paginate_items(FEEDS_URL, "Feeds", body):
            fid = f.get("Id")
            if fid is not None:
                names[int(fid)] = str(f.get("Name") or "")
    return names


def _fetch_geo_region_names(region_ids: List[int]) -> Dict[int, str]:
    if not region_ids:
        return {}
    abs_ids = sorted({abs(int(r)) for r in region_ids})
    names: Dict[int, str] = {}
    for part in _chunked([str(x) for x in abs_ids], 500):
        body = {
            "method": "getGeoRegions",
            "params": {
                "SelectionCriteria": {"RegionIds": [int(x) for x in part]},
                "FieldNames": ["GeoRegionId", "GeoRegionName"],
            },
        }
        try:
            result = _direct_post(DICTIONARIES_URL, body)
            for row in result.get("GeoRegions") or []:
                rid = row.get("GeoRegionId")
                if rid is not None:
                    names[int(rid)] = str(row.get("GeoRegionName") or f"#{rid}")
        except RuntimeError as e:
            print(f"  [lime_direct] getGeoRegions skip: {e}")
            break
    out: Dict[int, str] = {}
    for rid in region_ids:
        abs_rid = abs(int(rid))
        api_name = names.get(abs_rid, f"#{abs_rid}")
        out[int(rid)] = _geo_region_label(abs_rid, api_name)
    return out


def _format_regions_display(region_ids: List[int], geo_names: Dict[int, str]) -> str:
    if not region_ids:
        return ""
    positives = sorted({int(r) for r in region_ids if int(r) > 0})
    negatives = sorted({int(r) for r in region_ids if int(r) < 0}, key=abs)
    if 225 in positives and negatives:
        excluded = [geo_names.get(r, _geo_region_label(abs(r), f"#{abs(r)}")) for r in negatives]
        return "Россия − " + ", − ".join(excluded)
    if 225 in positives:
        other_pos = [p for p in positives if p != 225]
        if other_pos:
            excluded = [geo_names.get(p, _geo_region_label(p, f"#{p}")) for p in other_pos]
            return "Россия − " + ", − ".join(excluded)
        return "Россия"
    parts: List[str] = []
    for rid in sorted(region_ids, key=lambda x: (0 if x > 0 else 1, abs(x))):
        name = geo_names.get(int(rid), _geo_region_label(abs(int(rid)), f"#{abs(int(rid))}"))
        if int(rid) < 0:
            parts.append(f"− {name}")
        else:
            parts.append(name)
    return ", ".join(parts)


def _fetch_smart_targets(campaign_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Нацеленность смарт-объявлений (smartadtargets) — аудиторные сегменты, на которые
    настроены смарт-баннеры. Поля по докам API v5 (biplane/yandex-direct SmartAdTargetFieldEnum):
    Name, Audience (VISITED_PRODUCT_PAGE / INTERESTED_IN_SIMILAR_PRODUCTS / ALL_SEGMENTS),
    ConditionType, State. Плюс булев offerRetargeting (ConditionType == OFFER_RETARGETING).
    На отказе полных полей — откат на минимальный набор, чтобы не потерять детект офферного ретаргета."""
    out: Dict[str, Dict[str, Any]] = {
        cid: {"offerRetargeting": False, "targets": []} for cid in campaign_ids
    }
    full = ["Id", "CampaignId", "AdGroupId", "Name", "State", "ConditionType", "Audience"]
    minimal = ["Id", "CampaignId", "State", "ConditionType"]

    def _body(fields: List[str], ids: List[str]) -> Dict[str, Any]:
        return {
            "method": "get",
            "params": {
                "SelectionCriteria": {"CampaignIds": [int(x) for x in ids]},
                "FieldNames": fields,
            },
        }

    for part in _chunked(campaign_ids, 2):
        try:
            rows = list(_paginate_items(SMARTADTARGETS_URL, "SmartAdTargets", _body(full, part)))
        except RuntimeError as e:
            print(f"  [lime_direct] smartadtargets полные поля: {e}; откат на офферный флаг")
            try:
                rows = list(_paginate_items(SMARTADTARGETS_URL, "SmartAdTargets", _body(minimal, part)))
            except RuntimeError as e2:
                print(f"  [lime_direct] smartadtargets skip: {e2}")
                break
        for row in rows:
            cid = str(row.get("CampaignId", ""))
            if cid not in out:
                continue
            state = str(row.get("State", "")) or None
            ctype = str(row.get("ConditionType", "")) or None
            if ctype == "OFFER_RETARGETING" and state == "ON":
                out[cid]["offerRetargeting"] = True
            audience = row.get("Audience")
            name = row.get("Name")
            if audience or name:
                out[cid]["targets"].append({
                    "name": name,
                    "audience": audience,
                    "conditionType": ctype,
                    "state": state,
                })
    return out


def _fetch_goals_map(counter_ids: List[int]) -> Dict[int, str]:
    if not counter_ids:
        return {}
    names: Dict[int, str] = {}
    for part in _chunked([str(x) for x in counter_ids], 10):
        body = {
            "method": "get",
            "params": {
                "SelectionCriteria": {"CounterIds": [int(x) for x in part]},
                "FieldNames": ["Id", "Name"],
            },
        }
        try:
            result = _direct_post(GOALS_URL, body)
            for g in result.get("Goals") or []:
                gid = g.get("Id")
                if gid is not None:
                    names[int(gid)] = str(g.get("Name") or "")
        except RuntimeError as e:
            print(f"  [lime_direct] goals.get skip: {e}")
            break
    return names


def _build_campaign_settings(
    campaign_id: str,
    base: Dict[str, Any],
    adgroups: List[Dict[str, Any]],
    bidmodifiers: List[Dict[str, Any]],
    audience_targets: List[Dict[str, Any]],
    retargeting_names: Dict[int, str],
    autotargeting: Dict[str, Any],
    feed_names: Dict[int, str],
    geo_names: Dict[int, str],
    goal_names: Dict[int, str],
    offer_retargeting: bool,
    synced_at: str,
    smart_targets: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    regions: set[int] = set()
    offer_groups: List[Dict[str, Any]] = []
    offer_retargeting_on = offer_retargeting
    for ag in adgroups:
        for rid in ag.get("regionIds") or []:
            regions.add(int(rid))
        for rid in ag.get("restrictedRegionIds") or []:
            rid_int = int(rid)
            regions.add(-abs(rid_int) if rid_int > 0 else rid_int)
        if ag.get("offerRetargeting") == "YES":
            offer_retargeting_on = True
        ag_type = str(ag.get("type") or "")
        feed_id = ag.get("feedId")
        if ag_type == "SMART_AD_GROUP" or feed_id is not None:
            offer_groups.append({
                "adGroupId": ag.get("id"),
                "name": ag.get("name") or "—",
                "type": ag_type,
                "feedId": int(feed_id) if feed_id is not None else None,
                "feedName": feed_names.get(int(feed_id)) if feed_id is not None else None,
            })

    retargeting_mods = [
        m for m in bidmodifiers if m.get("type") == "RETARGETING_ADJUSTMENT"
    ]
    other_mods = [m for m in bidmodifiers if m.get("type") != "RETARGETING_ADJUSTMENT"]

    for m in bidmodifiers:
        rid = m.get("regionId")
        if rid is not None:
            regions.add(int(rid))
            m["detail"] = geo_names.get(int(rid), f"Регион {rid}")

    for m in retargeting_mods:
        if m.get("detail") and m["detail"].isdigit():
            lid = int(m["detail"])
            m["detail"] = retargeting_names.get(lid, m["detail"])

    strategy = dict(base.get("strategy") or {})
    if strategy.get("priorityGoals"):
        strategy["priorityGoals"] = [g for g in strategy["priorityGoals"] if int(g) != 13]
    if strategy.get("priorityGoalsDetails"):
        strategy["priorityGoalsDetails"] = [
            g for g in strategy["priorityGoalsDetails"] if int(g.get("goalId", 0)) != 13
        ]
    for ch_key in ("search", "network"):
        ch = strategy.get(ch_key)
        if not isinstance(ch, dict):
            continue
        gids = ch.get("goalIds") or []
        ch["goalLabels"] = [
            goal_names.get(int(g), f"Цель #{g}") for g in gids if g is not None
        ]
    pkg = strategy.get("package")
    if isinstance(pkg, dict) and pkg.get("goalIds"):
        pkg["goalLabels"] = [
            goal_names.get(int(g), f"Цель #{g}") for g in pkg["goalIds"] if g is not None
        ]
    priority = strategy.get("priorityGoals") or []
    if priority:
        strategy["priorityGoalLabels"] = [
            goal_names.get(int(g), f"Цель #{g}") for g in priority if g is not None
        ]

    sorted_regions = sorted(regions)
    region_names = [
        {"id": rid, "name": geo_names.get(rid, f"#{abs(rid)}")}
        for rid in sorted_regions
    ]
    region_display = _format_regions_display(sorted_regions, geo_names)

    audience_view = []
    for t in audience_targets:
        lid = t.get("retargetingListId")
        audience_view.append({
            "id": t.get("id"),
            "adGroupId": t.get("adGroupId"),
            "name": None,
            "retargetingListId": lid,
            "retargetingListName": retargeting_names.get(int(lid)) if lid is not None else None,
            "state": t.get("state"),
        })

    ag_negative_kw = sorted({
        str(kw) for ag in adgroups for kw in (ag.get("negativeKeywords") or []) if kw
    })
    smart_targets_clean = [
        t for t in (smart_targets or []) if t.get("audience") or t.get("name")
    ]

    settings = {
        "strategy": strategy,
        "audience": {
            "targets": audience_view,
            "retargetingModifiers": retargeting_mods,
        },
        "targeting": {
            **(base.get("targeting") or {}),
            "regions": sorted_regions,
            "regionNames": region_names,
            "regionDisplay": region_display,
            "offerRetargeting": offer_retargeting_on,
            "smartTargets": smart_targets_clean,
            "negativeKeywordsGroups": ag_negative_kw,
            "keywordsCount": int(autotargeting.get("keywordsCount", 0) or 0),
            "autotargeting": {
                **autotargeting,
                "totalGroups": len(adgroups),
            },
            "adGroupsTotal": len(adgroups),
        },
        "bidModifiers": {
            "total": len(other_mods),
            "items": other_mods,
        },
        "offerTargeting": {
            "hasFeedGroups": len(offer_groups) > 0,
            "groups": offer_groups,
        },
        "meta": {
            **(base.get("meta") or {}),
            "goalNames": {str(k): v for k, v in goal_names.items()},
            "syncedAt": synced_at,
        },
    }
    return settings


_SETTINGS_UPSERT_SQL = """
    INSERT INTO lime_campaign_settings (campaign_id, campaign_name, settings, synced_at)
    VALUES (%(campaign_id)s, %(campaign_name)s, %(settings)s::jsonb, NOW())
    ON CONFLICT (campaign_id) DO UPDATE SET
       campaign_name = EXCLUDED.campaign_name,
       settings = EXCLUDED.settings,
       synced_at = NOW()
"""


def _upsert_campaign_settings(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    with psycopg2.connect(_pg_url()) as conn:
        with conn.cursor() as cur:
            for row in rows:
                cur.execute(
                    _SETTINGS_UPSERT_SQL,
                    {
                        "campaign_id": row["campaign_id"],
                        "campaign_name": row.get("campaign_name"),
                        "settings": json.dumps(row["settings"], ensure_ascii=False),
                    },
                )
        conn.commit()
    return len(rows)


def _fetch_stats_meta_fallback(campaign_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not campaign_ids:
        return {}
    sql = """
        SELECT DISTINCT ON (campaign_id)
            campaign_id, campaign_name, campaign_type, state, status
        FROM lime_direct_stats
        WHERE campaign_id = ANY(%s)
        ORDER BY campaign_id, date DESC
    """
    out: Dict[str, Dict[str, Any]] = {}
    try:
        with psycopg2.connect(_pg_url()) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (campaign_ids,))
                for cid, cname, ctype, state, status in cur.fetchall():
                    out[str(cid)] = {
                        "campaign_name": cname,
                        "meta": {
                            "campaignType": ctype,
                            "state": state,
                            "status": status,
                        },
                    }
    except Exception as e:
        print(f"  [lime_direct] stats meta fallback skip: {e}")
    return out


def _sync_campaign_settings(campaign_ids: List[str], names: Dict[str, str]) -> int:
    if not campaign_ids:
        return 0

    synced_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"[lime_direct] настройки кампаний ({len(campaign_ids)})...")

    base_map = _fetch_campaigns_for_settings(campaign_ids)
    counter_ids = list(base_map.pop("_counter_ids", []) or [])

    missing_meta = [
        cid
        for cid in campaign_ids
        if not (base_map.get(cid, {}).get("meta") or {}).get("campaignType")
    ]
    if missing_meta:
        for cid, fb in _fetch_stats_meta_fallback(missing_meta).items():
            if cid not in base_map:
                base_map[cid] = {**fb, "strategy": {}, "targeting": {}}
            else:
                base_map[cid].setdefault("meta", {}).update(
                    {k: v for k, v in (fb.get("meta") or {}).items() if v}
                )
                if not base_map[cid].get("campaign_name"):
                    base_map[cid]["campaign_name"] = fb.get("campaign_name")

    adgroups_map = _fetch_adgroups_by_campaign(campaign_ids)
    bidmodifiers_map = _fetch_bidmodifiers_by_campaign(campaign_ids)
    audience_map = _fetch_audiencetargets_by_campaign(campaign_ids)
    autotargeting_map = _fetch_autotargeting_by_campaign(campaign_ids, adgroups_map)

    list_ids: set[int] = set()
    feed_ids: set[int] = set()
    for cid in campaign_ids:
        for t in audience_map.get(cid, []):
            lid = t.get("retargetingListId")
            if lid is not None:
                list_ids.add(int(lid))
        for m in bidmodifiers_map.get(cid, []):
            if m.get("type") == "RETARGETING_ADJUSTMENT" and m.get("detail") and str(m["detail"]).isdigit():
                list_ids.add(int(m["detail"]))
        for ag in adgroups_map.get(cid, []):
            fid = ag.get("feedId")
            if fid is not None:
                feed_ids.add(int(fid))

    retargeting_names = _fetch_retargetinglists_map(sorted(list_ids))
    feed_names = _fetch_feeds_map(sorted(feed_ids))

    region_ids: set[int] = set()
    goal_ids_needed: set[int] = set()
    # Все коллекции ниже — через _as_list: API Директа отдаёт их то списком, то
    # обёрткой {"Items": [...]}. При обёртке `for x in dict` перебирает КЛЮЧИ, и
    # int('Items') роняет весь синк настроек. Обёртку уже чинили для CounterIds и
    # PriorityGoals (aa5a584), но эти места пропустили — настройки не синхронизировались
    # молча, state/campaign_type оставались заполнены у 1173 из 1603 строк.
    for cid in campaign_ids:
        for ag in adgroups_map.get(cid, []):
            for rid in _as_list(ag.get("regionIds")):
                region_ids.add(int(rid))
            for rid in _as_list(ag.get("restrictedRegionIds")):
                rid_int = int(rid)
                region_ids.add(-abs(rid_int) if rid_int > 0 else rid_int)
        for m in bidmodifiers_map.get(cid, []):
            rid = m.get("regionId")
            if rid is not None:
                region_ids.add(int(rid))
        base = base_map.get(cid, {})
        strat = base.get("strategy") or {}
        for ch_key in ("search", "network"):
            ch = strat.get(ch_key) or {}
            for gid in _as_list(ch.get("goalIds")):
                goal_ids_needed.add(int(gid))
        pkg = strat.get("package") or {}
        for gid in _as_list(pkg.get("goalIds")):
            goal_ids_needed.add(int(gid))
        for gid in _as_list(strat.get("priorityGoals")):
            goal_ids_needed.add(int(gid))
        for pg in _as_list(strat.get("priorityGoalsDetails")):
            if isinstance(pg, dict) and pg.get("goalId") is not None:
                goal_ids_needed.add(int(pg["goalId"]))

    geo_names = _fetch_geo_region_names(sorted(region_ids))
    goal_names = _fetch_goals_map(counter_ids)
    smart_map = _fetch_smart_targets(campaign_ids)

    rows: List[Dict[str, Any]] = []
    for cid in campaign_ids:
        base = base_map.get(cid, {"meta": {}, "strategy": {}, "targeting": {}})
        if names.get(cid):
            base["campaign_name"] = names[cid]
        settings = _build_campaign_settings(
            cid,
            base,
            adgroups_map.get(cid, []),
            bidmodifiers_map.get(cid, []),
            audience_map.get(cid, []),
            retargeting_names,
            autotargeting_map.get(cid, {"enabledGroups": 0, "totalGroups": 0, "categories": []}),
            feed_names,
            geo_names,
            goal_names,
            smart_map.get(cid, {}).get("offerRetargeting", False),
            synced_at,
            smart_targets=smart_map.get(cid, {}).get("targets", []),
        )
        rows.append({
            "campaign_id": cid,
            "campaign_name": names.get(cid) or base.get("campaign_name"),
            "settings": settings,
        })

    n = _upsert_campaign_settings(rows)
    print(f"[lime_direct] upsert {n} строк в lime_campaign_settings")
    return n


_UPSERT_SQL = """
    INSERT INTO lime_direct_stats
      (date, campaign_id, campaign_name, client_login,
       impressions, clicks, cost,
       avg_effective_bid, avg_traffic_volume,
       avg_impression_position, avg_click_position,
       bounce_rate, avg_pageviews,
       weekly_budget, daily_budget, target_cpa,
       conversions,
       state, status, campaign_type, updated_at)
    VALUES
      (%(date)s, %(campaign_id)s, %(campaign_name)s, %(client_login)s,
       %(impressions)s, %(clicks)s, %(cost)s,
       %(avg_effective_bid)s, %(avg_traffic_volume)s,
       %(avg_impression_position)s, %(avg_click_position)s,
       %(bounce_rate)s, %(avg_pageviews)s,
       %(weekly_budget)s, %(daily_budget)s, %(target_cpa)s,
       %(conversions)s::jsonb,
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
    goal_ids, id_to_key = _parse_goals()

    print(f"[lime_direct] отчёт {date_from} — {date_to} ({client_login})")
    if goal_ids:
        print(f"[lime_direct] цели LSC: {', '.join(goal_ids)}")
    report_rows = _fetch_report(date_from, date_to, goal_ids, id_to_key)
    print(f"[lime_direct] получено {len(report_rows)} строк отчёта")
    if not report_rows:
        return 0

    campaign_ids = sorted({r["campaign_id"] for r in report_rows})
    campaign_names = {r["campaign_id"]: r["campaign_name"] for r in report_rows}
    campaigns = _fetch_campaigns(campaign_ids)
    print(f"[lime_direct] стратегии/бюджеты по {len(campaigns)} кампаниям")

    try:
        _sync_campaign_settings(campaign_ids, campaign_names)
    except Exception as e:
        # С traceback, а не только текстом: «invalid literal for int(): 'Items'»
        # повторялся неделями, и по одному сообщению нельзя было понять, в какой
        # из веток разбора настроек он возникает. Настройки при этом молча не
        # синхронизировались — state/campaign_type оставались неполными.
        print(f"[lime_direct] WARN: настройки кампаний не синхронизированы: {e}")
        traceback.print_exc()

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
            "conversions": json.dumps(r.get("conversions") or {}, ensure_ascii=False),
        })

    n = _upsert(merged)
    print(f"[lime_direct] upsert {n} строк в lime_direct_stats")
    return n


if __name__ == "__main__":
    sync_lime_direct(days_back=int(os.environ.get("LIME_DIRECT_DAYS_BACK", "7")))
