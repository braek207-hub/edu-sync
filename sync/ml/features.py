"""Чистые трансформации фич ML-скоринга EDU. Без побочных эффектов — тестируются
отдельно. Оркестрация чтения/записи — в sync/ml_features_build.py."""

import json
from datetime import date, datetime
from typing import Any, Optional


def load_admission_deadlines(path: str) -> list[date]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return [datetime.strptime(d, "%Y-%m-%d").date() for d in cfg["deadlines"]]


def days_to_deadline(created: date, deadlines: list[date]) -> int:
    """Дней до ближайшего дедлайна ≥ created. Если все в прошлом — дней до последнего
    (отрицательное)."""
    future = [d for d in deadlines if d >= created]
    if future:
        return (min(future) - created).days
    return (max(deadlines) - created).days


_CAT_NULLS = {"", "(not set)", "not_set", "--", "0", "unknown"}


def clean_cat(s: Optional[str]) -> Optional[str]:
    v = (s or "").strip()
    return None if v.lower() in _CAT_NULLS else v


def derive_labels(lead: dict, today: date, maturity_days: int = 90) -> dict:
    created = lead["created_date"]
    age = (today - created).days
    is_matured = age >= maturity_days
    paid = bool(lead.get("is_paid"))
    days_to_pay = None
    if paid and lead.get("payment_date"):
        days_to_pay = (lead["payment_date"] - created).days
    return {
        # финальная метка известна только для созревших когорт; иначе цензура
        "label_paid": paid if (is_matured or paid) else None,
        "label_connected": bool(lead.get("is_connected")),
        "label_deal": bool(lead.get("is_deal")),
        "is_matured": is_matured,
        "amount": lead.get("amount") if paid else None,
        "days_to_pay": days_to_pay,
    }


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def build_feature_rows(
    leads: list[dict],
    behavior_by_client: dict[str, dict],
    deadlines: list[date],
    today: date,
) -> list[dict]:
    """Строки под upsert_lead_features. `behavior_by_client[client_id]` = агрегат
    поведения (см. load_vuz_behavior_frame): visits, visit_days, avg_duration_sec,
    bounce_rate, page_depth, device, source. `repeat_lead` — считаем по частоте client_id."""
    freq: dict[str, int] = {}
    for ld in leads:
        cid = clean_cat(ld.get("client_id"))
        if cid:
            freq[cid] = freq.get(cid, 0) + 1

    rows: list[dict] = []
    for ld in leads:
        cid = clean_cat(ld.get("client_id"))
        beh = behavior_by_client.get(cid) if cid else None
        created = ld["created_date"]
        conn = ld.get("connection_date")
        ttc = (conn - created).days if conn else None

        feats = {
            "f__audience": clean_cat(ld.get("audience")),
            "f__b24_grad_year": clean_cat(ld.get("b24_grad_year")),
            "f__b24_edu_level": clean_cat(ld.get("b24_edu_level")),
            "f__city_ip_segment": clean_cat(ld.get("city_ip_segment")),
            "f__direction": clean_cat(ld.get("direction")),
            "f__product_group": clean_cat(ld.get("product_group")),
            "f__utm_source": clean_cat(ld.get("utm_source")),
            "f__created_dow": created.weekday(),
            "f__created_hour": int(ld.get("created_hour") or 0),
            "f__days_to_deadline": days_to_deadline(created, deadlines),
            "f__beh_visits": _num(beh and beh.get("visits")),
            "f__beh_visit_days": _num(beh and beh.get("visit_days")),
            "f__beh_avg_duration_sec": _num(beh and beh.get("avg_duration_sec")),
            "f__beh_bounce_rate": _num(beh and beh.get("bounce_rate")),
            "f__beh_page_depth": _num(beh and beh.get("page_depth")),
            "f__beh_device": clean_cat(beh.get("device")) if beh else None,
            "f__beh_source": clean_cat(beh.get("source")) if beh else None,
            "f__missing_behavior": 0 if beh else 1,
            "f__repeat_lead": (freq.get(cid, 0) if cid else 0),
            "f__time_to_connection_days": ttc,
            "f__dispatcher": clean_cat(ld.get("dispatcher")),
            "f__responsible": clean_cat(ld.get("responsible")),
        }
        labels = derive_labels(ld, today=today)
        rows.append({
            "lead_id": ld["lead_id"],
            "client_id": cid,
            "land": ld["land"],
            "created_date": created,
            "features": feats,
            **labels,
        })
    return rows
