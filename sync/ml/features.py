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


def _before_lead_visits(visits: list[dict], cutoff: date) -> list[dict]:
    return [v for v in visits if v["visit_date"] < cutoff]


def _weighted(visits: list[dict], key: str) -> float:
    total_visits = sum(_num(v.get("visits")) for v in visits)
    if total_visits <= 0:
        return 0.0
    return sum(_num(v.get(key)) * _num(v.get("visits")) for v in visits) / total_visits


def _top_visit_day(visits: list[dict]) -> Optional[dict]:
    if not visits:
        return None
    return max(visits, key=lambda v: _num(v.get("visits")))


def build_feature_rows(
    leads: list[dict],
    behavior_dated: dict[str, list[dict]],
    deadlines: list[date],
    today: date,
) -> list[dict]:
    """Строки под upsert_lead_features. `behavior_dated[client_id]` = список per-date
    визитов (visit_date, visits, avg_duration_sec, bounce_rate, page_depth, device,
    source). Все beh_*/timing-фичи time-aware: считаются ТОЛЬКО по визитам ДО заявки
    (visit_date < created_ts.date() если created_ts есть, иначе < created_date) —
    защита от post-lead утечки. `repeat_lead` — считаем по частоте client_id."""
    freq: dict[str, int] = {}
    for ld in leads:
        cid = clean_cat(ld.get("client_id"))
        if cid:
            freq[cid] = freq.get(cid, 0) + 1

    rows: list[dict] = []
    for ld in leads:
        cid = clean_cat(ld.get("client_id"))
        created = ld["created_date"]
        created_ts = ld.get("created_ts")
        cutoff = created_ts.date() if created_ts else created
        conn = ld.get("connection_date")
        ttc = (conn - created).days if conn else None

        all_visits = behavior_dated.get(cid, []) if cid else []
        before = _before_lead_visits(all_visits, cutoff)
        before_days = sorted({v["visit_date"] for v in before})
        top_day = _top_visit_day(before)

        connected_ts = ld.get("connected_ts")
        mins_to_connection = (
            (connected_ts - created_ts).total_seconds() / 60
            if created_ts and connected_ts else None
        )

        feats = {
            "f__audience": clean_cat(ld.get("audience")),
            "f__b24_grad_year": clean_cat(ld.get("b24_grad_year")),
            "f__b24_edu_level": clean_cat(ld.get("b24_edu_level")),
            "f__city_ip_segment": clean_cat(ld.get("city_ip_segment")),
            "f__direction": clean_cat(ld.get("direction")),
            "f__campaign_id": clean_cat(ld.get("campaign_id")),
            "f__product_group": clean_cat(ld.get("product_group")),
            "f__utm_source": clean_cat(ld.get("utm_source")),
            "f__created_dow": created.weekday(),
            # created_ts = MSK-настенное время (to_iso_datetime) в timestamptz. .hour
            # корректен, пока синк-запись и сборка фич в одном TZ (CI/Supabase = UTC).
            # Не запускать сборку с PGTZ=Europe/Moscow — сдвинет час на смещение UTC.
            "f__created_hour": created_ts.hour if created_ts else 0,
            "f__days_to_deadline": days_to_deadline(created, deadlines),
            "f__beh_visits": sum(_num(v.get("visits")) for v in before),
            "f__beh_visit_days": len(before_days),
            "f__beh_avg_duration_sec": _weighted(before, "avg_duration_sec"),
            "f__beh_bounce_rate": _weighted(before, "bounce_rate"),
            "f__beh_page_depth": _weighted(before, "page_depth"),
            "f__beh_device": clean_cat(top_day.get("device")) if top_day else None,
            "f__beh_source": clean_cat(top_day.get("source")) if top_day else None,
            "f__missing_behavior": 0 if before else 1,
            "f__repeat_lead": (freq.get(cid, 0) if cid else 0),
            "f__visits_before_lead": sum(_num(v.get("visits")) for v in before),
            "f__sessions_before": len(before_days),
            "f__days_since_first_touch": (cutoff - before_days[0]).days if before_days else 0,
            "f__had_repeat_visit": 1 if len(before_days) > 1 else 0,
            "f__mins_to_connection": mins_to_connection,
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
