"""Polina Repik: заказы Битрикса → Panda-BI ingest.

Их эндпоинт отдаёт заказы, ИЗМЕНЁННЫЕ за вчерашний день (по DATE_UPDATE), без
лимита, в формате 1-в-1 с нашим ingest-роутом. Тянем pull-моделью (раннер за
рубежом — обходит фильтрацию РКН к AWS) и POST-им в /api/ingest/polinarepik,
который делает идемпотентный upsert по order_id + замену items.

GitHub Actions: .github/workflows/sync-polinarepik-orders.yml
Local: python -m sync.polinarepik_orders
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import requests

SOURCE_URL = os.environ.get(
    "POLINAREPIK_ORDERS_API_URL", "https://polinarepik.com/api/metrikaorder/get"
).strip()
INGEST_URL = os.environ.get(
    "POLINAREPIK_INGEST_URL", "https://panda-bi.vercel.app/api/ingest/polinarepik"
).strip()
BATCH_SIZE = int(os.environ.get("POLINAREPIK_ORDERS_BATCH", "200"))
MAX_RETRIES = 4


def _require(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required env: {name}")
    return value


def fetch_orders(api_key: str) -> list[dict[str, Any]]:
    """GET заказов с их эндпоинта. Ретрай при таймауте/5xx (их сайт подвисает)."""
    backoff = 3
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                SOURCE_URL,
                headers={"X-API-Key": api_key, "Accept": "application/json"},
                timeout=(15, 60),
            )
        except requests.exceptions.RequestException as e:
            last_err = e
            print(f"  fetch attempt {attempt}/{MAX_RETRIES} failed: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        if resp.status_code in (500, 502, 503, 504):
            last_err = RuntimeError(f"HTTP {resp.status_code}")
            print(f"  fetch attempt {attempt}/{MAX_RETRIES}: HTTP {resp.status_code}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        if resp.status_code != 200:
            raise RuntimeError(f"Source API {resp.status_code}: {resp.text[:400]}")
        payload = resp.json()
        if payload.get("status") != "success":
            raise RuntimeError(f"Source API status != success: {str(payload)[:400]}")
        orders = (payload.get("data") or {}).get("orders") or []
        return orders
    raise RuntimeError(f"Source API: max retries ({last_err})")


def post_batch(orders: list[dict[str, Any]], token: str) -> dict[str, Any]:
    data = json.dumps({"orders": orders}, ensure_ascii=False).encode("utf-8")
    backoff = 3
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                INGEST_URL,
                data=data,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                timeout=(15, 90),
            )
        except requests.exceptions.RequestException as e:
            print(f"  ingest attempt {attempt}/{MAX_RETRIES} failed: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        if resp.status_code != 200:
            raise RuntimeError(f"Ingest {resp.status_code}: {resp.text[:400]}")
        return resp.json()
    raise RuntimeError("Ingest: max retries")


def main() -> int:
    print("=== Polinarepik Orders Sync START ===")
    api_key = _require("POLINAREPIK_ORDERS_API_KEY")
    token = _require("POLINAREPIK_INGEST_TOKEN")

    orders = fetch_orders(api_key)
    print(f"Fetched {len(orders)} orders (изменённые за вчера)")
    if not orders:
        print("Nothing to sync.")
        print("=== Polinarepik Orders Sync DONE ===")
        return 0

    total_upserted = 0
    all_skipped: list[Any] = []
    all_failed: list[Any] = []
    for i in range(0, len(orders), BATCH_SIZE):
        chunk = orders[i : i + BATCH_SIZE]
        res = post_batch(chunk, token)
        total_upserted += int(res.get("upserted") or 0)
        all_skipped.extend(res.get("skipped") or [])
        all_failed.extend(res.get("failed") or [])
        print(f"  batch {i // BATCH_SIZE + 1}: upserted={res.get('upserted')}")

    print(f"Total upserted: {total_upserted}/{len(orders)}")
    if all_skipped:
        print(f"Skipped: {json.dumps(all_skipped, ensure_ascii=False)}")
    if all_failed:
        print(f"Failed: {json.dumps(all_failed, ensure_ascii=False)}")
    print("=== Polinarepik Orders Sync DONE ===")
    # failed = заказ не записан (оставить сигнал в CI); skipped = штатно (нет order_id/date)
    return 1 if all_failed else 0


if __name__ == "__main__":
    sys.exit(main())
