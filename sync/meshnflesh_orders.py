"""Mesh-n-Flesh: заказы InSales → Panda-BI ingest.

Pull напрямую из InSales Admin API (HTTP Basic: API-ключ + пароль из админки,
Настройки → API). Тянем заказы, ИЗМЕНЁННЫЕ за окно (updated_since, дефолт 7 дней —
добирает поздние оплаты/смены статуса), маппим в общий ingest-формат Panda-BI
(тот же, что polinarepik/bjorn) и POST-им в /api/ingest/meshnflesh — идемпотентный
upsert по order_id + замена items.

Атрибуция: ЯТМ-тег на сайте пишет utm_*/yclid/ym_client_id в доп. поля заказа
InSales (fields_values, ключ = handle). См. docs/tracking/mesh-n-flesh в Panda-репо.

Секреты не заведены → мягкий выход 0 (не красить cron, пока Павел не положил ключи).

GitHub Actions: .github/workflows/sync-meshnflesh-orders.yml
Local: python -m sync.meshnflesh_orders  (env: MESHNFLESH_SYNC_DAYS для бэкфилла)
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

INGEST_URL = os.environ.get(
    "MESHNFLESH_INGEST_URL", "https://panda-bi.vercel.app/api/ingest/meshnflesh"
).strip()
SYNC_DAYS = int(os.environ.get("MESHNFLESH_SYNC_DAYS", "7"))
PER_PAGE = 100
BATCH_SIZE = 200
MAX_RETRIES = 4

# Идентификаторы доп. полей заказа (Настройки → Оформление заказа → Поля заказа).
ATTRIBUTION_HANDLES = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "yclid",
    "ym_client_id",
}


def _get_with_retry(url: str, auth: tuple[str, str], params: dict[str, Any]) -> requests.Response:
    backoff = 3
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, auth=auth, params=params, timeout=(15, 60))
        except requests.exceptions.RequestException as e:
            last_err = e
            print(f"  fetch attempt {attempt}/{MAX_RETRIES} failed: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        # 429 — InSales лимитирует (leaky bucket ~500 req / 5 мин): ждём и повторяем.
        if resp.status_code in (429, 500, 502, 503, 504):
            last_err = RuntimeError(f"HTTP {resp.status_code}")
            retry_after = int(resp.headers.get("Retry-After") or backoff)
            print(f"  fetch attempt {attempt}/{MAX_RETRIES}: HTTP {resp.status_code}, wait {retry_after}s")
            time.sleep(retry_after)
            backoff = min(backoff * 2, 60)
            continue
        if resp.status_code != 200:
            raise RuntimeError(f"InSales API {resp.status_code}: {resp.text[:400]}")
        return resp
    raise RuntimeError(f"InSales API: max retries ({last_err})")


def fetch_orders(host: str, api_key: str, password: str, since: datetime) -> list[dict[str, Any]]:
    """Все заказы, изменённые с `since` (постранично)."""
    url = f"https://{host}/admin/orders.json"
    auth = (api_key, password)
    orders: list[dict[str, Any]] = []
    page = 1
    while True:
        resp = _get_with_retry(
            url,
            auth,
            {
                "updated_since": since.strftime("%Y-%m-%d %H:%M"),
                "per_page": PER_PAGE,
                "page": page,
            },
        )
        chunk = resp.json()
        if not isinstance(chunk, list):
            raise RuntimeError(f"InSales API: ожидался список заказов, пришло {str(chunk)[:200]}")
        orders.extend(chunk)
        if len(chunk) < PER_PAGE:
            return orders
        page += 1


def _attribution_fields(order: dict[str, Any]) -> dict[str, str]:
    """utm_*/yclid/ym_client_id из доп. полей заказа (fields_values, ключ = handle)."""
    out: dict[str, str] = {}
    for fv in order.get("fields_values") or []:
        handle = str(fv.get("handle") or "").strip().lower()
        value = str(fv.get("value") or "").strip()
        if handle in ATTRIBUTION_HANDLES and value:
            out[handle] = value
    return out


def map_order(order: dict[str, Any]) -> dict[str, Any] | None:
    """InSales order → ingest-формат Panda-BI (lib/ingest/order-schema)."""
    order_id = order.get("number") or order.get("id")
    created_at = str(order.get("created_at") or "").strip()
    if not order_id or not created_at:
        return None

    # Статус: человекочитаемый кастомный (рус.), фолбэк — fulfillment_status.
    # «Отменен» должен долетать словом: хендлер отфильтровывает LIKE '%отмен%'.
    custom = order.get("custom_status") or {}
    status = str(custom.get("title") or "").strip()
    fulfillment = str(order.get("fulfillment_status") or "").strip()
    if not status:
        status = "Отменён" if fulfillment == "declined" else fulfillment

    attribution = _attribution_fields(order)

    items = []
    for line in order.get("order_lines") or []:
        quantity = line.get("quantity")
        price = line.get("sale_price", line.get("full_sale_price"))
        if quantity is None or price is None:
            continue
        items.append(
            {
                "product_id": str(line.get("sku") or line.get("product_id") or "") or None,
                "product_name": str(line.get("title") or "").strip() or None,
                "quantity": quantity,
                "price": price,
            }
        )

    return {
        "order_id": str(order_id),
        "order_date": created_at[:10],
        "status": status or None,
        # financial_status: paid / pending — хендлер считает оплатой ровно 'paid'.
        "payment_status": str(order.get("financial_status") or "").strip() or None,
        "amount": order.get("total_price"),
        "utm_source": attribution.get("utm_source"),
        "utm_medium": attribution.get("utm_medium"),
        "utm_campaign": attribution.get("utm_campaign"),
        "yclid": attribution.get("yclid"),
        "ym_client_id": attribution.get("ym_client_id"),
        "items": items or None,
    }


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
    print("=== Mesh-n-Flesh Orders Sync START ===")
    host = (os.environ.get("MESHNFLESH_INSALES_HOST") or "").strip()
    api_key = (os.environ.get("MESHNFLESH_INSALES_API_KEY") or "").strip()
    password = (os.environ.get("MESHNFLESH_INSALES_PASSWORD") or "").strip()
    token = (os.environ.get("MESHNFLESH_INGEST_TOKEN") or "").strip()

    missing = [
        name
        for name, value in [
            ("MESHNFLESH_INSALES_HOST", host),
            ("MESHNFLESH_INSALES_API_KEY", api_key),
            ("MESHNFLESH_INSALES_PASSWORD", password),
            ("MESHNFLESH_INGEST_TOKEN", token),
        ]
        if not value
    ]
    if missing:
        # Мягкий выход: секреты ещё не заведены — не красим cron ложной тревогой
        # (прецедент: красный по своей же причине перестают читать).
        print(f"Secrets not configured yet, skip: {', '.join(missing)}")
        print("=== Mesh-n-Flesh Orders Sync SKIPPED ===")
        return 0

    since = datetime.now(timezone.utc) - timedelta(days=SYNC_DAYS)
    raw = fetch_orders(host, api_key, password, since)
    print(f"Fetched {len(raw)} orders (updated_since {since:%Y-%m-%d %H:%M} UTC, окно {SYNC_DAYS} дн.)")

    orders = [m for m in (map_order(o) for o in raw) if m]
    dropped = len(raw) - len(orders)
    if dropped:
        print(f"Unmappable (без id/даты): {dropped}")
    if not orders:
        print("Nothing to sync.")
        print("=== Mesh-n-Flesh Orders Sync DONE ===")
        return 0

    total_upserted = 0
    all_rejected: list[Any] = []
    all_failed: list[Any] = []
    for i in range(0, len(orders), BATCH_SIZE):
        chunk = orders[i : i + BATCH_SIZE]
        res = post_batch(chunk, token)
        total_upserted += int(res.get("upserted") or 0)
        all_rejected.extend(res.get("rejected") or [])
        all_failed.extend(res.get("failed") or [])
        print(f"  batch {i // BATCH_SIZE + 1}: upserted={res.get('upserted')}")

    print(f"Total upserted: {total_upserted}/{len(orders)}")
    if all_rejected:
        print(f"Rejected by schema: {json.dumps(all_rejected, ensure_ascii=False)}")
    if all_failed:
        print(f"Failed: {json.dumps(all_failed, ensure_ascii=False)}")
    print("=== Mesh-n-Flesh Orders Sync DONE ===")
    # failed = заказ не записан (сигнал в CI); rejected = кривой заказ, виден в логе
    return 1 if all_failed else 0


if __name__ == "__main__":
    sys.exit(main())
