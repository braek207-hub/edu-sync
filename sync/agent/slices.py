# -*- coding: utf-8 -*-
"""
sync/agent/slices.py — срезы Директа в разрезе кампаний.

Два решения ради объёма БД:
  1. НЕДЕЛЬНАЯ грань, не дневная. Корректировки считаются за окно целиком, дневная
     детализация им не нужна; динамика ловится на уровне кампании в edu_agent_facts.
  2. Три отдельных двумерных разреза (кампания × регион, × площадка, × устройство),
     а не декартово произведение: полное произведение за 90 дней — 17,6 млн комбинаций.

Хвост сворачивается в slice_key='other': регион с двумя кликами всё равно получит
корректировку 0 после сжатия к базе, а строк порождает столько же, сколько Москва.
"""

from datetime import date, datetime, timedelta
from typing import Any, Dict, List

MIN_CLICKS_KEEP = 30


def to_week_start(date_iso: str) -> str:
    """Понедельник недели, которой принадлежит дата."""
    d = date_iso if isinstance(date_iso, date) else datetime.fromisoformat(str(date_iso)).date()
    return (d - timedelta(days=d.weekday())).isoformat()


def build_sliced_facts(report_rows: List[Dict[str, Any]], slice_kind: str) -> List[Dict[str, Any]]:
    """Сворачивает дневные строки отчёта в недельные строки среза."""
    acc: Dict[tuple, Dict[str, Any]] = {}
    for r in report_rows:
        week = to_week_start(r["date"])
        key = (week, str(r["campaign_id"]), slice_kind, str(r["slice_key"]))
        slot = acc.setdefault(key, {
            "week_start": week,
            "campaign_id": str(r["campaign_id"]),
            "slice_kind": slice_kind,
            "slice_key": str(r["slice_key"]),
            "cost": 0.0, "clicks": 0, "impressions": 0, "conversions": 0,
        })
        slot["cost"] += float(r.get("cost") or 0.0)
        slot["clicks"] += int(r.get("clicks") or 0)
        slot["impressions"] += int(r.get("impressions") or 0)
        slot["conversions"] += int(r.get("conversions") or 0)
    return sorted(acc.values(), key=lambda x: (x["week_start"], x["campaign_id"], x["slice_key"]))


def collapse_tail(rows: List[Dict[str, Any]], min_clicks: int = MIN_CLICKS_KEEP) -> List[Dict[str, Any]]:
    """Сворачивает малозначимые ключи в 'other' внутри своей кампании и недели."""
    kept: List[Dict[str, Any]] = []
    tail: Dict[tuple, Dict[str, Any]] = {}
    for r in rows:
        if int(r.get("clicks") or 0) >= min_clicks:
            kept.append(r)
            continue
        key = (r["week_start"], r["campaign_id"], r["slice_kind"])
        slot = tail.setdefault(key, {
            "week_start": r["week_start"],
            "campaign_id": r["campaign_id"],
            "slice_kind": r["slice_kind"],
            "slice_key": "other",
            "cost": 0.0, "clicks": 0, "impressions": 0, "conversions": 0,
        })
        slot["cost"] += float(r.get("cost") or 0.0)
        slot["clicks"] += int(r.get("clicks") or 0)
        slot["impressions"] += int(r.get("impressions") or 0)
        slot["conversions"] += int(r.get("conversions") or 0)
    return sorted(kept + list(tail.values()),
                  key=lambda x: (x["week_start"], x["campaign_id"], x["slice_key"]))
