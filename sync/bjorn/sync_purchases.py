# BJORN: по-заказная атрибуция из ecommerce Метрики → bjorn_metrika_purchases.
# ym:s:purchaseID = номер заказа Bitrix (проверено пробой 02.09.2026), джойн с bjorn_orders
# по order_id. Аналог sync_metrica_purchases в sync/polinarepik.py.
from __future__ import annotations

import argparse
import time

import requests

from sync.bjorn.common import SupabaseRest, add_date_range_args, env_required, resolve_date_range

STAT = "https://api-metrika.yandex.net/stat/v1/data"
TABLE = "bjorn_metrika_purchases"
SKIP_ORDER_IDS = {"", "(not set)", "0", "--"}


def stat_get(params: dict, headers: dict) -> dict:
    backoff = 2
    for attempt in range(6):
        resp = requests.get(STAT, params=params, headers=headers, timeout=180)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in {429, 500, 502, 503, 504} and attempt < 5:
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        raise RuntimeError(f"stat API {resp.status_code}: {resp.text[:500]}")
    raise RuntimeError("retry loop exhausted")


def fetch_purchases(date_from: str, date_to: str) -> list[dict]:
    headers = {"Authorization": f"OAuth {env_required('METRICA_TOKEN')}"}
    counter = env_required("METRICA_COUNTER_ID")
    dimensions = ",".join(
        [
            "ym:s:purchaseID",
            "ym:s:date",
            "ym:s:clientID",
            "ym:s:lastsignTrafficSource",
            "ym:s:lastsignSourceEngine",
            "ym:s:lastsignUTMSource",
            "ym:s:lastsignUTMMedium",
            "ym:s:lastsignUTMCampaign",
        ]
    )

    raw: list[dict] = []
    offset = 1
    while True:
        body = stat_get(
            {
                "ids": counter,
                "metrics": "ym:s:ecommercePurchases,ym:s:ecommerceRevenue",
                "dimensions": dimensions,
                "date1": date_from,
                "date2": date_to,
                "attribution": "lastsign",
                "accuracy": "full",
                "proposed_accuracy": "false",
                "lang": "ru",
                "limit": 10000,
                "offset": offset,
            },
            headers,
        )
        data = body.get("data", [])
        for item in data:
            dims = [str(d.get("name") or "") for d in item.get("dimensions", [])]
            metrics = item.get("metrics", [])
            raw.append(
                {
                    "order_id": dims[0].strip(),
                    "purchase_date": dims[1],
                    "client_id": dims[2],
                    "traffic_source": dims[3],
                    "source_engine": dims[4],
                    "utm_source": dims[5],
                    "utm_medium": dims[6],
                    "utm_campaign": dims[7],
                    "purchases": int(metrics[0] or 0),
                    "revenue": float(metrics[1] or 0),
                }
            )
        if len(data) < 10000:
            break
        offset += len(data)

    # Один заказ может дать несколько строк (разные визиты) — оставляем самую раннюю дату.
    by_order: dict[str, dict] = {}
    for row in raw:
        oid = row["order_id"]
        if oid in SKIP_ORDER_IDS or row["purchases"] <= 0:
            continue
        kept = by_order.get(oid)
        if kept is None or row["purchase_date"] < kept["purchase_date"]:
            by_order[oid] = row
    return list(by_order.values())


def main() -> None:
    parser = argparse.ArgumentParser(description="BJORN: атрибуция заказов из ecommerce Метрики")
    add_date_range_args(parser)
    date_from, date_to = resolve_date_range(parser.parse_args())

    rows = fetch_purchases(date_from, date_to)
    print(f"{TABLE}: {date_from}..{date_to} → {len(rows)} заказов")

    supabase = SupabaseRest()
    # delete_date_range из common фильтрует колонку `date`; здесь колонка purchase_date.
    supabase.request(
        "DELETE",
        TABLE,
        params={"purchase_date": [f"gte.{date_from}", f"lte.{date_to}"]},
        headers={"Prefer": "return=minimal"},
    )
    supabase.upsert(TABLE, rows, on_conflict="order_id")


if __name__ == "__main__":
    main()
