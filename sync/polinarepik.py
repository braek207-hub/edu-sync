"""Polina Repik: Yandex Direct + Metrika (clientID) → Supabase.

GitHub Actions: .github/workflows/sync-polinarepik.yml
Local: python -m sync.polinarepik
"""
from __future__ import annotations

import csv
import io
import os
import re
import sys
import time
from datetime import date, timedelta
from typing import Any

import requests

from sync.db import (
    delete_polinarepik_metrica_from,
    delete_polinarepik_metrica_purchases_from,
    delete_polinarepik_metrica_sources_from,
    upsert_polinarepik_direct_stats,
    upsert_polinarepik_metrica_purchases,
    upsert_polinarepik_metrica_sources,
    upsert_polinarepik_metrica_visits,
)

# Non-secret defaults (env overrides for local dev)
DIRECT_CLIENT_LOGIN = "polinarepik-wear"
METRICA_COUNTER_ID = "100764399"
METRICA_ATTRIBUTION = "lastsign"
DEFAULT_SYNC_DAYS = 60

# Цели Метрики воронки (счётчик 100764399)
METRICA_GOAL_CART = "512437503"      # Ecommerce: добавление в корзину
METRICA_GOAL_CHECKOUT = "371515249"  # Инициация оформления заказа

DIRECT_API_URL = "https://api.direct.yandex.com/json/v5/reports"
METRICA_API_URL = "https://api-metrika.yandex.net/stat/v1/data"
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 600
MAX_POLL_ATTEMPTS = 30


def _env(name: str, fallback: str = "") -> str:
    return (os.environ.get(name) or fallback).strip()


def yandex_token() -> str:
    """Single OAuth token for Yandex Direct + Metrika."""
    for name in ("POLINAREPIK_YANDEX_TOKEN", "POLINAREPIK_DIRECT_TOKEN", "POLINAREPIK_METRICA_TOKEN"):
        value = _env(name)
        if value:
            return value
    raise RuntimeError("Missing Yandex OAuth token: set POLINAREPIK_YANDEX_TOKEN")


def direct_client_login() -> str:
    return _env("POLINAREPIK_DIRECT_CLIENT_LOGIN", DIRECT_CLIENT_LOGIN)


def metrica_counter_id() -> str:
    return _env("POLINAREPIK_METRICA_COUNTER_ID", METRICA_COUNTER_ID)


def metrica_attribution() -> str:
    return _env("POLINAREPIK_METRICA_ATTRIBUTION", METRICA_ATTRIBUTION)


def campaign_platform(name: str) -> str:
    t = (name or "").lower()
    if "рся" in t:
        return "rsya"
    if "мк" in t:
        return "mc"
    if "поиск" in t:
        return "search"
    return "other"


def normalize_campaign_id(raw: str) -> str:
    text = (raw or "").strip()
    if not text or text in {"(not set)", "not_set", "--"}:
        return ""
    if text.replace(".", "", 1).isdigit():
        return str(int(float(text)))
    m = re.match(r"^(\d{6,})", text)
    return m.group(1) if m else text


def _direct_headers(login: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {yandex_token()}",
        "Client-Login": login,
        "Accept-Language": "ru",
        "processingMode": "auto",
        "returnMoneyInMicros": "false",
        "skipReportHeader": "true",
        "skipColumnHeader": "true",
        "skipReportSummary": "true",
    }


def fetch_direct_report(date_from: str, date_to: str, login: str) -> list[dict[str, Any]]:
    body = {
        "params": {
            "SelectionCriteria": {"DateFrom": date_from, "DateTo": date_to},
            "FieldNames": ["Date", "CampaignId", "CampaignName", "Impressions", "Clicks", "Cost"],
            "ReportName": f"polinarepik_{login}_{date_from}_{date_to}",
            "ReportType": "CAMPAIGN_PERFORMANCE_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES",
            "IncludeDiscount": "NO",
        }
    }
    headers = _direct_headers(login)
    resp = None
    for _ in range(MAX_POLL_ATTEMPTS):
        resp = requests.post(
            DIRECT_API_URL,
            json=body,
            headers=headers,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
        if resp.status_code == 200:
            break
        if resp.status_code in (201, 202):
            time.sleep(max(30, min(int(resp.headers.get("retryIn", 60)), 120)))
            continue
        raise RuntimeError(f"Direct API {resp.status_code}: {resp.text[:400]}")
    else:
        raise RuntimeError("Direct API: max retries")

    rows: list[dict[str, Any]] = []
    reader = csv.reader(io.StringIO(resp.text.lstrip("\ufeff")), delimiter="\t")
    for parts in reader:
        if len(parts) < 6:
            continue
        campaign_id = str(parts[1]).strip()
        if not campaign_id or campaign_id == "--":
            continue
        campaign_name = str(parts[2]).strip()
        rows.append(
            {
                "date": str(parts[0]).strip(),
                "campaign_id": campaign_id,
                "campaign_name": campaign_name,
                "source_type": campaign_platform(campaign_name),
                "cost": float(parts[5] or 0),
                "clicks": int(float(parts[4] or 0)),
                "impressions": int(float(parts[3] or 0)),
            }
        )
    return rows


def sync_direct(days_back: int) -> int:
    login = direct_client_login()
    if not login:
        raise RuntimeError("Direct client login missing")
    today = date.today()
    date_from = (today - timedelta(days=days_back)).isoformat()
    date_to = today.isoformat()
    print(f"Direct: {date_from} — {date_to} [{login}]")
    rows = fetch_direct_report(date_from, date_to, login)
    print(f"  rows: {len(rows)}")
    if not rows:
        return 0
    return upsert_polinarepik_direct_stats(rows)


def _metrica_get(params: dict[str, Any], token: str) -> dict[str, Any]:
    headers = {"Authorization": f"OAuth {token}"}
    backoff = 2
    for attempt in range(6):
        resp = requests.get(METRICA_API_URL, params=params, headers=headers, timeout=120)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in {429, 500, 502, 503, 504} and attempt < 5:
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        raise RuntimeError(f"Metrica API {resp.status_code}: {resp.text[:400]}")
    raise RuntimeError("Metrica API: max retries")


def fetch_metrica_client_visits(date_from: str, date_to: str) -> list[dict[str, Any]]:
    token = yandex_token()
    counter_id = metrica_counter_id()
    if not counter_id:
        raise RuntimeError("Metrika counter ID missing")
    attribution = metrica_attribution()

    # clientID-гранулярность (для атрибуции заказов) + воронка/поведение. ВАЖНО: source_detail
    # (lastsignSourceEngineName) НЕСОВМЕСТИМ с clientID (вырождает выдачу) → отдельный
    # source-level запрос fetch_metrica_sources.
    dimensions = ",".join(
        [
            "ym:s:date",
            "ym:s:clientID",
            "ym:s:lastSignTrafficSource",
            "ym:s:lastSignUTMSource",
            "ym:s:lastSignUTMMedium",
            "ym:s:lastSignUTMCampaign",
        ]
    )
    metrics = ",".join(
        [
            "ym:s:visits",
            "ym:s:bounceRate",   # отказы, % (0..100)
            "ym:s:pageDepth",    # глубина просмотра, стр/визит
            f"ym:s:goal{METRICA_GOAL_CART}reaches",      # добавление в корзину
            f"ym:s:goal{METRICA_GOAL_CHECKOUT}reaches",  # инициация оформления
        ]
    )

    # На ключ копим: визиты + взвешенные (visits) bounce/depth + достижения целей.
    aggregate: dict[tuple, dict[str, float]] = {}
    limit = 100000
    offset = 1

    while True:
        params = {
            "ids": counter_id,
            "metrics": metrics,
            "dimensions": dimensions,
            "date1": date_from,
            "date2": date_to,
            "accuracy": "full",
            "proposed_accuracy": "false",
            "attribution": attribution,
            "lang": "ru",
            "limit": limit,
            "offset": offset,
        }
        payload = _metrica_get(params, token)
        data = payload.get("data", [])
        if not data:
            break

        for record in data:
            dims = [str(d.get("name", "")).strip() for d in record.get("dimensions", [])]
            if len(dims) < 2:
                continue
            row_date, client_id = dims[0], dims[1]
            if not row_date or not client_id or client_id in {"(not set)", "0"}:
                continue
            traffic_source = dims[2] if len(dims) > 2 else ""
            utm_source = (dims[3] if len(dims) > 3 else "") or ""
            utm_medium = (dims[4] if len(dims) > 4 else "") or ""
            utm_campaign = normalize_campaign_id(dims[5] if len(dims) > 5 else "") or ""
            mets = record.get("metrics") or []
            visits = int(float(mets[0] or 0)) if len(mets) > 0 else 0
            if visits <= 0:
                continue
            bounce = float(mets[1] or 0) if len(mets) > 1 else 0.0
            depth = float(mets[2] or 0) if len(mets) > 2 else 0.0
            cart = int(float(mets[3] or 0)) if len(mets) > 3 else 0
            checkout = int(float(mets[4] or 0)) if len(mets) > 4 else 0

            key = (row_date, client_id, traffic_source, utm_source, utm_medium, utm_campaign)
            acc = aggregate.get(key)
            if acc is None:
                acc = {"visits": 0, "bounce_w": 0.0, "depth_w": 0.0, "cart": 0, "checkout": 0}
                aggregate[key] = acc
            acc["visits"] += visits
            acc["bounce_w"] += bounce * visits
            acc["depth_w"] += depth * visits
            acc["cart"] += cart
            acc["checkout"] += checkout

        if len(data) < limit:
            break
        offset += limit

    out: list[dict[str, Any]] = []
    for key, acc in sorted(aggregate.items()):
        row_date, client_id, traffic_source, utm_source, utm_medium, utm_campaign = key
        v = acc["visits"]
        out.append(
            {
                "date": row_date,
                "client_id": client_id,
                "traffic_source": traffic_source,
                "utm_source": utm_source,
                "utm_medium": utm_medium,
                "utm_campaign": utm_campaign,
                "visits": v,
                "bounce_rate": round(acc["bounce_w"] / v, 2) if v else 0.0,
                "page_depth": round(acc["depth_w"] / v, 2) if v else 0.0,
                "cart_reaches": acc["cart"],
                "checkout_reaches": acc["checkout"],
            }
        )
    return out


def fetch_metrica_sources(date_from: str, date_to: str) -> list[dict[str, Any]]:
    """Source-level (БЕЗ clientID): детальный источник Метрики (source_detail =
    lastsignSourceEngineName: Яндекс: Директ / Google / ВКонтакте…) по (категория, utm).
    Отдельный запрос, т.к. SourceEngineName несовместим с clientID. Для маппинга Канала."""
    token = yandex_token()
    counter_id = metrica_counter_id()
    if not counter_id:
        raise RuntimeError("Metrika counter ID missing")
    attribution = metrica_attribution()

    dimensions = ",".join(
        [
            "ym:s:date",
            "ym:s:lastSignTrafficSource",
            "ym:s:lastsignSourceEngineName",
            "ym:s:lastSignUTMSource",
            "ym:s:lastSignUTMMedium",
            "ym:s:lastSignUTMCampaign",
        ]
    )
    aggregate: dict[tuple, int] = {}
    limit = 100000
    offset = 1
    while True:
        params = {
            "ids": counter_id,
            "metrics": "ym:s:visits",
            "dimensions": dimensions,
            "date1": date_from,
            "date2": date_to,
            "accuracy": "full",
            "proposed_accuracy": "false",
            "attribution": attribution,
            "lang": "ru",
            "limit": limit,
            "offset": offset,
        }
        payload = _metrica_get(params, token)
        data = payload.get("data", [])
        if not data:
            break
        for record in data:
            dims = [str(d.get("name", "")).strip() for d in record.get("dimensions", [])]
            if not dims or not dims[0]:
                continue
            traffic_source = dims[1] if len(dims) > 1 else ""
            source_detail = (dims[2] if len(dims) > 2 else "") or ""
            utm_source = (dims[3] if len(dims) > 3 else "") or ""
            utm_medium = (dims[4] if len(dims) > 4 else "") or ""
            utm_campaign = normalize_campaign_id(dims[5] if len(dims) > 5 else "") or ""
            visits = int(float((record.get("metrics") or [0])[0] or 0))
            if visits <= 0:
                continue
            key = (dims[0], traffic_source, source_detail, utm_source, utm_medium, utm_campaign)
            aggregate[key] = aggregate.get(key, 0) + visits
        if len(data) < limit:
            break
        offset += limit

    return [
        {
            "date": d, "traffic_source": ts, "source_detail": sd,
            "utm_source": us, "utm_medium": um, "utm_campaign": uc, "visits": v,
        }
        for (d, ts, sd, us, um, uc), v in sorted(aggregate.items())
    ]


def sync_metrica(days_back: int) -> int:
    today = date.today()
    date_from = (today - timedelta(days=days_back)).isoformat()
    date_to = today.isoformat()
    print(f"Metrica clientID: {date_from} — {date_to}")
    rows = fetch_metrica_client_visits(date_from, date_to)
    print(f"  rows: {len(rows)}")
    if not rows:
        return 0
    deleted = delete_polinarepik_metrica_from(date_from)
    print(f"  deleted from {date_from}: {deleted}")
    return upsert_polinarepik_metrica_visits(rows)


def sync_metrica_sources(days_back: int) -> int:
    today = date.today()
    date_from = (today - timedelta(days=days_back)).isoformat()
    date_to = today.isoformat()
    print(f"Metrica sources (source_detail): {date_from} — {date_to}")
    rows = fetch_metrica_sources(date_from, date_to)
    print(f"  rows: {len(rows)}")
    if not rows:
        return 0
    deleted = delete_polinarepik_metrica_sources_from(date_from)
    print(f"  deleted from {date_from}: {deleted}")
    return upsert_polinarepik_metrica_sources(rows)


def _metrica_text(value: str) -> str:
    text = (value or "").strip()
    if not text or text in {"(not set)", "not_set", "--", "None", "none"}:
        return ""
    return text


def fetch_metrica_purchases(date_from: str, date_to: str) -> list[dict[str, Any]]:
    token = yandex_token()
    counter_id = metrica_counter_id()
    if not counter_id:
        raise RuntimeError("Metrika counter ID missing")
    attribution = metrica_attribution()

    dimensions = ",".join(
        [
            "ym:s:date",
            "ym:s:purchaseID",
            "ym:s:clientID",
            "ym:s:lastSignTrafficSource",
            "ym:s:lastSignUTMSource",
            "ym:s:lastSignUTMMedium",
            "ym:s:lastSignUTMCampaign",
        ]
    )

    by_order: dict[str, dict[str, Any]] = {}
    limit = 100000
    offset = 1

    while True:
        params = {
            "ids": counter_id,
            "metrics": "ym:s:ecommercePurchases,ym:s:ecommerceRevenue",
            "dimensions": dimensions,
            "date1": date_from,
            "date2": date_to,
            "accuracy": "full",
            "proposed_accuracy": "false",
            "attribution": attribution,
            "lang": "ru",
            "limit": limit,
            "offset": offset,
            "filters": "ym:s:ecommercePurchases>0",
        }
        payload = _metrica_get(params, token)
        data = payload.get("data", [])
        if not data:
            break

        for record in data:
            dims = [str(d.get("name", "")).strip() for d in record.get("dimensions", [])]
            if len(dims) < 2:
                continue
            row_date, order_id = dims[0], dims[1]
            if not row_date or not order_id or order_id in {"(not set)", "0", "--"}:
                continue
            client_id = _metrica_text(dims[2] if len(dims) > 2 else "")
            traffic_source = _metrica_text(dims[3] if len(dims) > 3 else "")
            utm_source = _metrica_text(dims[4] if len(dims) > 4 else "")
            utm_medium = _metrica_text(dims[5] if len(dims) > 5 else "")
            utm_campaign = normalize_campaign_id(_metrica_text(dims[6] if len(dims) > 6 else "")) or ""
            metrics = record.get("metrics") or [0, 0]
            purchases = int(float(metrics[0] or 0))
            revenue = float(metrics[1] or 0)
            if purchases <= 0:
                continue

            prev = by_order.get(order_id)
            if prev and prev["purchase_date"] > row_date:
                continue

            by_order[order_id] = {
                "order_id": order_id,
                "purchase_date": row_date,
                "client_id": client_id,
                "traffic_source": traffic_source,
                "utm_source": utm_source,
                "utm_medium": utm_medium,
                "utm_campaign": utm_campaign,
                "purchases": purchases,
                "revenue": revenue,
            }

        if len(data) < limit:
            break
        offset += limit

    return list(by_order.values())


def sync_metrica_purchases(days_back: int) -> int:
    today = date.today()
    date_from = (today - timedelta(days=days_back)).isoformat()
    date_to = today.isoformat()
    print(f"Metrica purchases: {date_from} — {date_to}")
    rows = fetch_metrica_purchases(date_from, date_to)
    print(f"  rows: {len(rows)}")
    if not rows:
        return 0
    deleted = delete_polinarepik_metrica_purchases_from(date_from)
    print(f"  deleted from {date_from}: {deleted}")
    return upsert_polinarepik_metrica_purchases(rows)


def main() -> int:
    print("=== Polinarepik Sync START ===")

    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL required")
        return 1

    days = int(os.environ.get("POLINAREPIK_SYNC_DAYS", str(DEFAULT_SYNC_DAYS)))
    errors: list[str] = []

    try:
        n = sync_direct(days)
        print(f"Direct upserted: {n}")
    except Exception as e:
        print(f"ERROR direct: {e}")
        errors.append(f"direct: {e}")

    try:
        n = sync_metrica(days)
        print(f"Metrica visits upserted: {n}")
    except Exception as e:
        print(f"ERROR metrica visits: {e}")
        errors.append(f"metrica visits: {e}")

    try:
        n = sync_metrica_sources(days)
        print(f"Metrica sources upserted: {n}")
    except Exception as e:
        print(f"ERROR metrica sources: {e}")
        errors.append(f"metrica sources: {e}")

    try:
        n = sync_metrica_purchases(days)
        print(f"Metrica purchases upserted: {n}")
    except Exception as e:
        print(f"ERROR metrica purchases: {e}")
        errors.append(f"metrica purchases: {e}")

    print("=== Polinarepik Sync DONE ===")

    if errors:
        print("Errors:", "; ".join(errors))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
