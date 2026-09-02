# Проба BJORN: по-заказная атрибуция из ecommerce Метрики.
# Вопрос: заполнен ли ym:s:purchaseID номером заказа Bitrix, и какой источник (lastsign)
# Метрика приписала каждой покупке. Read-only, только stat API.
from __future__ import annotations

import json
import os
import time

import requests

STAT = "https://api-metrika.yandex.net/stat/v1/data"
DATE1 = os.environ.get("PROBE_DATE1", "2026-07-25")
DATE2 = os.environ.get("PROBE_DATE2", "2026-08-31")


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


def main() -> None:
    token = os.environ["METRICA_TOKEN"]
    counter = os.environ["METRICA_COUNTER_ID"]
    headers = {"Authorization": f"OAuth {token}"}

    dimensions = ",".join(
        [
            "ym:s:purchaseID",
            "ym:s:date",
            "ym:s:lastsignTrafficSource",
            "ym:s:lastsignSourceEngine",
            "ym:s:lastsignUTMSource",
            "ym:s:lastsignUTMCampaign",
        ]
    )
    rows: list[dict] = []
    offset = 1
    while True:
        body = stat_get(
            {
                "ids": counter,
                "metrics": "ym:s:ecommercePurchases,ym:s:ecommerceRevenue",
                "dimensions": dimensions,
                "date1": DATE1,
                "date2": DATE2,
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
            dims = [d.get("name") for d in item.get("dimensions", [])]
            metrics = item.get("metrics", [])
            rows.append(
                {
                    "purchase_id": dims[0],
                    "date": dims[1],
                    "traffic_source": dims[2],
                    "source_engine": dims[3],
                    "utm_source": dims[4],
                    "utm_campaign": dims[5],
                    "purchases": metrics[0],
                    "revenue": metrics[1],
                }
            )
        if len(data) < 10000:
            break
        offset += len(data)

    print(f"total_rows={len(rows)} period={DATE1}..{DATE2}")
    print("ORDERS_JSON_BEGIN")
    print(json.dumps(rows, ensure_ascii=False))
    print("ORDERS_JSON_END")


if __name__ == "__main__":
    main()
