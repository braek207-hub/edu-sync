# -*- coding: utf-8 -*-
"""sync/lime_vk_ads.py — кабинет VK Реклама (ads.vk.com) → lime_vk_ads_stats.

Паритет с Директом: расход/клики/показы + конверсии по типам (jsonb). Валюта RUB —
конверсии валюты нет; spent как в кабинете (без НДС). Rate-limit строгий: батчи id,
паузы, ретрай 429, токен кэшируется на прогон.

ENV: DATABASE_URL, VK_CLIENT_ID, VK_CLIENT_SECRET, LIME_VK_DAYS_BACK (default 14).
Запуск: python -m sync.lime_vk_ads
"""
import json
from typing import Any, Dict, Tuple


def _num(v: Any) -> float:
    if v is None:
        return 0.0
    s = str(v).strip().replace(",", ".")
    try:
        return float(s) if s not in ("", "--") else 0.0
    except ValueError:
        return 0.0


def _int(v: Any) -> int:
    return int(round(_num(v)))


def parse_base_stats(api_json: Dict[str, Any]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """(date, campaign_id) → базовые метрики строки. Секция total игнорируется."""
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for item in api_json.get("items", []):
        cid = str(item.get("id", "")).strip()
        if not cid:
            continue
        for row in item.get("rows", []):
            date = str(row.get("date", "")).strip()
            base = row.get("base") or {}
            vk = base.get("vk") or {}
            out[(date, cid)] = {
                "shows": _int(base.get("shows")),
                "clicks": _int(base.get("clicks")),
                "spent": round(_num(base.get("spent")), 2),
                "goals_total": _int(base.get("goals")),
                "vk_result": _int(vk.get("result")),
            }
    return out


def parse_goal_stats(api_json: Dict[str, Any]) -> Dict[Tuple[str, str], Dict[str, Dict[str, Any]]]:
    """(date, campaign_id) → {goal: {count, value, view_through}}; строки одной цели суммируются."""
    out: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}
    for item in api_json.get("items", []):
        cid = str(item.get("id", "")).strip()
        if not cid:
            continue
        for row in item.get("rows", []):
            date = str(row.get("date", "")).strip()
            bucket = out.setdefault((date, cid), {})
            for g in row.get("goals", []):
                name = str(g.get("goal", "")).strip()
                if not name:
                    continue
                agg = bucket.setdefault(name, {"count": 0, "value": 0.0, "view_through": 0})
                agg["count"] += _int(g.get("count"))
                agg["value"] = round(agg["value"] + _num(g.get("value")), 2)
                agg["view_through"] += _int(g.get("view_through_count"))
    return out


def build_rows(base_map, goals_map, campaigns_meta) -> list:
    """Слить базу + конверсии + мету кампании в строки upsert. Ведёт база (только где есть статистика)."""
    rows = []
    for (date, cid), base in base_map.items():
        meta = campaigns_meta.get(cid, {})
        conv = goals_map.get((date, cid), {})
        rows.append({
            "date": date,
            "region": "ru",
            "campaign_id": cid,
            "campaign_name": meta.get("name"),
            "objective": meta.get("objective"),
            "status": meta.get("status"),
            "shows": base["shows"],
            "clicks": base["clicks"],
            "spent": base["spent"],
            "goals_total": base["goals_total"],
            "vk_result": base["vk_result"],
            "conversions": json.dumps(conv, ensure_ascii=False),
        })
    return rows
