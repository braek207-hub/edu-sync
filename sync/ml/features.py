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


def _session_ts(s: dict) -> datetime:
    """`visit_ts` может прийти как datetime (из БД) или ISO-строка (тесты/сериализация
    JSON). Python 3.11+ `datetime.fromisoformat` уже понимает пробел вместо `T`
    (формат Logs API `ym:s:dateTime`, см. sync/edu_visits_logs.py)."""
    v = s["visit_ts"]
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v))


def _before_lead_sessions(
    sessions: list[dict], created_ts: Optional[datetime], created_date: date
) -> list[dict]:
    """Per-visit сессии СТРОГО ДО момента заявки — без утечки. Если известен точный
    `created_ts` (timestamptz), сравниваем по времени (`visit_ts < created_ts`) —
    это и учитывает same-day визиты ДО заявки, отбрасывая same-day визиты ПОСЛЕ.
    Деградация (нет created_ts) — по дате (`visit_ts.date() < created_date`), как
    старый дневной cutoff, там same-day визиты неотличимы по порядку."""
    if created_ts is not None:
        return [s for s in sessions if _session_ts(s) < created_ts]
    return [s for s in sessions if _session_ts(s).date() < created_date]


def _last_session(sessions: list[dict]) -> Optional[dict]:
    if not sessions:
        return None
    return max(sessions, key=_session_ts)


def build_feature_rows(
    leads: list[dict],
    sessions: dict[str, list[dict]],
    deadlines: list[date],
    today: date,
) -> list[dict]:
    """Строки под upsert_lead_features. `sessions[client_id]` = список per-visit
    сессий Метрики Logs API (Task 3, edu_visit_sessions): visit_ts (datetime/ISO),
    visit_duration, bounce, page_views, is_new_user, utm_*, first_traffic_source,
    source_engine, direct_*, has_gclid, phone_model, network_type.

    Все beh_*/sess_*/timing-фичи time-aware: считаются ТОЛЬКО по визитам ДО заявки
    (Ф2 Logs API — `_before_lead_sessions`: `visit_ts < created_ts` если created_ts
    есть — это учитывает same-day визиты ДО заявки в отличие от старого дневного
    cutoff'а; деградация на `visit_ts.date() < created_date` для лидов без ts).
    `repeat_lead` — считаем по частоте client_id."""
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

        all_sessions = sessions.get(cid, []) if cid else []
        before = _before_lead_sessions(all_sessions, created_ts, created)
        before_days = sorted({_session_ts(s).date() for s in before})
        last = _last_session(before)
        n_before = len(before)

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
            # beh_* пересчитаны по per-visit сессиям (было: дневной агрегат Reporting API)
            "f__beh_visits": n_before,
            "f__beh_visit_days": len(before_days),
            "f__beh_avg_duration_sec": (
                sum(_num(s.get("visit_duration")) for s in before) / n_before if n_before else 0.0
            ),
            "f__beh_bounce_rate": (
                sum(_num(s.get("bounce")) for s in before) / n_before * 100 if n_before else 0.0
            ),
            "f__beh_page_depth": (
                sum(_num(s.get("page_views")) for s in before) / n_before if n_before else 0.0
            ),
            "f__beh_device": clean_cat(last.get("device_category")) if last else None,
            "f__beh_source": clean_cat(last.get("source_engine")) if last else None,
            "f__missing_behavior": 0 if before else 1,
            "f__repeat_lead": (freq.get(cid, 0) if cid else 0),
            "f__visits_before_lead": n_before,
            "f__sessions_before": len(before_days),
            "f__days_since_first_touch": (cutoff - before_days[0]).days if before_days else 0,
            "f__had_repeat_visit": 1 if len(before_days) > 1 else 0,
            "f__mins_to_connection": mins_to_connection,
            "f__time_to_connection_days": ttc,
            "f__dispatcher": clean_cat(ld.get("dispatcher")),
            "f__responsible": clean_cat(ld.get("responsible")),
            # Ф2 (Task 4/5) — per-visit сессии Logs API. Категориальные — значение
            # ПОСЛЕДНЕЙ pre-lead сессии (ближайший к заявке сигнал); is_new_user/
            # has_gclid — max (флаг "было хоть раз" за pre-lead окно).
            "f__sess_is_new_user": max((_num(s.get("is_new_user")) for s in before), default=0.0),
            "f__sess_utm_source": clean_cat(last.get("utm_source")) if last else None,
            "f__sess_utm_medium": clean_cat(last.get("utm_medium")) if last else None,
            "f__sess_utm_campaign": clean_cat(last.get("utm_campaign")) if last else None,
            "f__sess_utm_content": clean_cat(last.get("utm_content")) if last else None,
            "f__sess_utm_term": clean_cat(last.get("utm_term")) if last else None,
            "f__sess_first_traffic_source": clean_cat(last.get("first_traffic_source")) if last else None,
            "f__sess_source_engine": clean_cat(last.get("source_engine")) if last else None,
            "f__sess_direct_platform_type": clean_cat(last.get("direct_platform_type")) if last else None,
            "f__sess_direct_condition_type": clean_cat(last.get("direct_condition_type")) if last else None,
            "f__sess_direct_phrase_bucket": clean_cat(last.get("direct_phrase")) if last else None,
            "f__sess_has_gclid": max((_num(s.get("has_gclid")) for s in before), default=0.0),
            "f__sess_phone_model": clean_cat(last.get("phone_model")) if last else None,
            "f__sess_network_type": clean_cat(last.get("network_type")) if last else None,
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
