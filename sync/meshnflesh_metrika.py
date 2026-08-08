"""Mesh-n-Flesh: Yandex Metrika (счётчик 82116769) → Supabase.

Зеркало метричных блоков sync/polinarepik.py, без Direct (кабинет подключим,
когда одобрят заявку API) и без clientID-блока (атрибуция заказов идёт по
utm-полям самого заказа InSales + каскаду purchases, lookback по визитам не нужен):
  • sources: source-level визиты + отказы/глубина + цели корзина/оформление
    → meshnflesh_metrica_sources (delete-from + upsert);
  • purchases: ecommerce по-заказно (purchaseID = номер заказа InSales, проверено)
    → meshnflesh_metrica_purchases (PK order_id).

Токен — общий OAuth аккаунта approve.digital (доступ к счётчику выдан 2026-08-08).
Секреты не заведены → мягкий выход 0 (не красить cron).

GitHub Actions: .github/workflows/sync-meshnflesh-metrika.yml
Local: python -m sync.meshnflesh_metrika  (env: MESHNFLESH_METRIKA_SYNC_DAYS)
"""
from __future__ import annotations

import os
import re
import sys
import time
from datetime import date, timedelta
from typing import Any, Dict, List

import psycopg2.extras
import requests

from sync.db import get_connection

METRICA_COUNTER_ID = "82116769"
METRICA_ATTRIBUTION = "lastsign"
DEFAULT_SYNC_DAYS = 60

# Цели воронки счётчика 82116769 (management API, 2026-08-08)
METRICA_GOAL_CART = "194805961"      # Ecommerce: добавление в корзину (e_cart)
METRICA_GOAL_CHECKOUT = "560966657"  # Автоцель: начало оформления заказа (a_begin_checkout)

METRICA_API_URL = "https://api-metrika.yandex.net/stat/v1/data"


def _env(name: str, fallback: str = "") -> str:
    return (os.environ.get(name) or fallback).strip()


def normalize_campaign_id(raw: str) -> str:
    text = (raw or "").strip()
    if not text or text in {"(not set)", "not_set", "--"}:
        return ""
    if text.replace(".", "", 1).isdigit():
        return str(int(float(text)))
    m = re.match(r"^(\d{6,})", text)
    return m.group(1) if m else text


def _metrica_text(value: str) -> str:
    text = (value or "").strip()
    if not text or text in {"(not set)", "not_set", "--", "None", "none"}:
        return ""
    return text


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


def fetch_metrica_sources(token: str, date_from: str, date_to: str) -> list[dict[str, Any]]:
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
    metrics = ",".join(
        [
            "ym:s:visits",
            "ym:s:bounceRate",
            "ym:s:pageDepth",
            f"ym:s:goal{METRICA_GOAL_CART}reaches",
            f"ym:s:goal{METRICA_GOAL_CHECKOUT}reaches",
        ]
    )
    aggregate: dict[tuple, dict[str, float]] = {}
    limit = 100000
    offset = 1
    while True:
        payload = _metrica_get(
            {
                "ids": METRICA_COUNTER_ID,
                "metrics": metrics,
                "dimensions": dimensions,
                "date1": date_from,
                "date2": date_to,
                "accuracy": "full",
                "proposed_accuracy": "false",
                "attribution": METRICA_ATTRIBUTION,
                "lang": "ru",
                "limit": limit,
                "offset": offset,
            },
            token,
        )
        data = payload.get("data", [])
        if not data:
            break
        for record in data:
            dims = [str(d.get("name", "")).strip() for d in record.get("dimensions", [])]
            if not dims or not dims[0]:
                continue
            traffic_source = dims[1] if len(dims) > 1 else ""
            source_detail = _metrica_text(dims[2] if len(dims) > 2 else "")
            utm_source = _metrica_text(dims[3] if len(dims) > 3 else "")
            utm_medium = _metrica_text(dims[4] if len(dims) > 4 else "")
            utm_campaign = normalize_campaign_id(_metrica_text(dims[5] if len(dims) > 5 else ""))
            mets = record.get("metrics") or []
            visits = int(float(mets[0] or 0)) if len(mets) > 0 else 0
            if visits <= 0:
                continue
            bounce = float(mets[1] or 0) if len(mets) > 1 else 0.0
            depth = float(mets[2] or 0) if len(mets) > 2 else 0.0
            cart = int(float(mets[3] or 0)) if len(mets) > 3 else 0
            checkout = int(float(mets[4] or 0)) if len(mets) > 4 else 0
            key = (dims[0], traffic_source, source_detail, utm_source, utm_medium, utm_campaign)
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
    for (d, ts, sd, us, um, uc), acc in sorted(aggregate.items()):
        v = acc["visits"]
        out.append(
            {
                "date": d, "traffic_source": ts, "source_detail": sd,
                "utm_source": us, "utm_medium": um, "utm_campaign": uc, "visits": v,
                "bounce_rate": round(acc["bounce_w"] / v, 2) if v else 0.0,
                "page_depth": round(acc["depth_w"] / v, 2) if v else 0.0,
                "cart_reaches": acc["cart"],
                "checkout_reaches": acc["checkout"],
            }
        )
    return out


def fetch_metrica_purchases(token: str, date_from: str, date_to: str) -> list[dict[str, Any]]:
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
        payload = _metrica_get(
            {
                "ids": METRICA_COUNTER_ID,
                "metrics": "ym:s:ecommercePurchases,ym:s:ecommerceRevenue",
                "dimensions": dimensions,
                "date1": date_from,
                "date2": date_to,
                "accuracy": "full",
                "proposed_accuracy": "false",
                "attribution": METRICA_ATTRIBUTION,
                "lang": "ru",
                "limit": limit,
                "offset": offset,
                "filters": "ym:s:ecommercePurchases>0",
            },
            token,
        )
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
                "client_id": _metrica_text(dims[2] if len(dims) > 2 else ""),
                "traffic_source": _metrica_text(dims[3] if len(dims) > 3 else ""),
                "utm_source": _metrica_text(dims[4] if len(dims) > 4 else ""),
                "utm_medium": _metrica_text(dims[5] if len(dims) > 5 else ""),
                "utm_campaign": normalize_campaign_id(_metrica_text(dims[6] if len(dims) > 6 else "")),
                "purchases": purchases,
                "revenue": revenue,
            }
        if len(data) < limit:
            break
        offset += limit

    return list(by_order.values())


# ── Писатели (delete-from + upsert, как у polinarepik) ──


def delete_sources_from(date_from: str) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM meshnflesh_metrica_sources WHERE date >= %s", (date_from,))
            deleted = cur.rowcount
        conn.commit()
    return deleted


def upsert_sources(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    sql = """
        INSERT INTO meshnflesh_metrica_sources (
            date, traffic_source, source_detail, utm_source, utm_medium, utm_campaign, visits,
            bounce_rate, page_depth, cart_reaches, checkout_reaches, updated_at
        )
        VALUES (
            %(date)s, %(traffic_source)s, %(source_detail)s, %(utm_source)s,
            %(utm_medium)s, %(utm_campaign)s, %(visits)s,
            %(bounce_rate)s, %(page_depth)s, %(cart_reaches)s, %(checkout_reaches)s, NOW()
        )
        ON CONFLICT (date, traffic_source, source_detail, utm_source, utm_medium, utm_campaign) DO UPDATE SET
            visits           = EXCLUDED.visits,
            bounce_rate      = EXCLUDED.bounce_rate,
            page_depth       = EXCLUDED.page_depth,
            cart_reaches     = EXCLUDED.cart_reaches,
            checkout_reaches = EXCLUDED.checkout_reaches,
            updated_at       = NOW()
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=500)
        conn.commit()
    return len(rows)


def delete_purchases_from(date_from: str) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM meshnflesh_metrica_purchases WHERE purchase_date >= %s", (date_from,)
            )
            deleted = cur.rowcount
        conn.commit()
    return deleted


def upsert_purchases(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    sql = """
        INSERT INTO meshnflesh_metrica_purchases (
            order_id, purchase_date, client_id, traffic_source, utm_source,
            utm_medium, utm_campaign, purchases, revenue, updated_at
        )
        VALUES (
            %(order_id)s, %(purchase_date)s, %(client_id)s, %(traffic_source)s, %(utm_source)s,
            %(utm_medium)s, %(utm_campaign)s, %(purchases)s, %(revenue)s, NOW()
        )
        ON CONFLICT (order_id) DO UPDATE SET
            purchase_date  = EXCLUDED.purchase_date,
            client_id      = COALESCE(NULLIF(EXCLUDED.client_id, ''), meshnflesh_metrica_purchases.client_id),
            traffic_source = EXCLUDED.traffic_source,
            utm_source     = EXCLUDED.utm_source,
            utm_medium     = EXCLUDED.utm_medium,
            utm_campaign   = EXCLUDED.utm_campaign,
            purchases      = EXCLUDED.purchases,
            revenue        = EXCLUDED.revenue,
            updated_at     = NOW()
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=500)
        conn.commit()
    return len(rows)


def main() -> int:
    print("=== Mesh-n-Flesh Metrika Sync START ===")
    token = _env("MESHNFLESH_YANDEX_TOKEN")
    if not token:
        print("Secrets not configured yet, skip: MESHNFLESH_YANDEX_TOKEN")
        print("=== Mesh-n-Flesh Metrika Sync SKIPPED ===")
        return 0
    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL required")
        return 1

    days = int(_env("MESHNFLESH_METRIKA_SYNC_DAYS", str(DEFAULT_SYNC_DAYS)))
    today = date.today()
    date_from = (today - timedelta(days=days)).isoformat()
    date_to = today.isoformat()
    errors: list[str] = []

    try:
        print(f"Metrica sources: {date_from} — {date_to}")
        rows = fetch_metrica_sources(token, date_from, date_to)
        print(f"  rows: {len(rows)}")
        if rows:
            deleted = delete_sources_from(date_from)
            print(f"  deleted from {date_from}: {deleted}")
            print(f"  upserted: {upsert_sources(rows)}")
    except Exception as e:
        print(f"ERROR metrica sources: {e}")
        errors.append(f"sources: {e}")

    try:
        print(f"Metrica purchases: {date_from} — {date_to}")
        rows = fetch_metrica_purchases(token, date_from, date_to)
        print(f"  rows: {len(rows)}")
        if rows:
            deleted = delete_purchases_from(date_from)
            print(f"  deleted from {date_from}: {deleted}")
            print(f"  upserted: {upsert_purchases(rows)}")
    except Exception as e:
        print(f"ERROR metrica purchases: {e}")
        errors.append(f"purchases: {e}")

    print("=== Mesh-n-Flesh Metrika Sync DONE ===")
    if errors:
        print("Errors:", "; ".join(errors))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
