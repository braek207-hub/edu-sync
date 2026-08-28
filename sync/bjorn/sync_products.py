"""Товарная воронка Bjorn: показ товара → клик → корзина → покупка, из stat API Метрики.

Грань — день × модель атрибуции × источник × кампания × объявление × товар. Ступень
«просмотр карточки» здесь не собирается: в stat API её нет вовсе, она приходит отдельным
шагом sync_card_views через Logs API.

Имена всех девяти метрик проверены живым API 28.08.2026 (probe_bjorn_products_ads13.py):
каждая принята по отдельности и все вместе в шестимерной группировке. Итоги ступеней у трёх
моделей атрибуции ОДИНАКОВЫ — модель меняет распределение по источникам, а не сумму.
"""

from __future__ import annotations

import argparse
import time
from collections import defaultdict
from typing import Any

import requests

from .common import (
    SupabaseRest,
    add_date_range_args,
    clean_text,
    env_optional,
    env_required,
    money,
    normalize_campaign_id,
    normalize_source,
    parse_int,
    resolve_date_range,
)

API_URL = "https://api-metrika.yandex.net/stat/v1/data"
ATTRIBUTIONS_DEFAULT = "automatic,first,lastsign"
TABLE = "bjorn_product_daily"
ON_CONFLICT = "date,attribution,source,campaign_id,ad_id,product_id,product_name"

METRICS = [
    "ym:s:productImpressions",
    "ym:s:productImpressionsUniq",
    "ym:s:productClicks",
    "ym:s:productClicksUniq",
    "ym:s:productBasketsQuantity",
    "ym:s:productBasketsUniq",
    "ym:s:productPurchasedQuantity",
    "ym:s:productPurchasedUniq",
    "ym:s:productPurchasedPrice",
]

# Порядок метрик в ответе = порядок в METRICS; имена колонок витрины в том же порядке.
METRIC_COLUMNS = [
    "product_impressions",
    "product_impressions_uniq",
    "product_clicks",
    "product_clicks_uniq",
    "baskets",
    "baskets_uniq",
    "purchased",
    "purchased_uniq",
]


def metrica_get(params: dict[str, Any], token: str) -> dict[str, Any]:
    headers = {"Authorization": f"OAuth {token}"}
    backoff = 2
    for attempt in range(6):
        response = requests.get(API_URL, params=params, headers=headers, timeout=180)
        if response.status_code == 200:
            return response.json()
        if response.status_code in {429, 500, 502, 503, 504} and attempt < 5:
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        raise RuntimeError(f"Metrica API error {response.status_code}: {response.text[:500]}")
    raise RuntimeError("Metrica API retry loop exhausted")


def dim_text(value: Any) -> str:
    """Метрика отдаёт литерал "None" для пустого измерения, и clean_text его не чистит.

    Без этой чистки в витрину попадёт кампания с именем «None» — ровно та ловушка, на которой
    у LIME молча умерла стадия обогащения витрины.
    """
    text = clean_text(value)
    return "" if text in {"None", "null", "--", "(not set)"} else text


def strip_banner(value: Any) -> str:
    """stat API отдаёт «M-16448294678», Директ и Logs API — голый «16448294678».

    Ключ объявления должен быть один на все три источника, иначе вкладки не сойдутся.
    """
    text = dim_text(value)
    return text[2:] if text.startswith("M-") else text


def fetch_attribution(date_from: str, date_to: str, attribution: str) -> list[dict[str, Any]]:
    token = env_required("METRICA_TOKEN")
    counter_id = env_required("METRICA_COUNTER_ID")
    dimensions = ",".join(
        [
            "ym:s:date",
            f"ym:s:{attribution}TrafficSource",
            f"ym:s:{attribution}UTMCampaign",
            f"ym:s:{attribution}DirectBanner",
            "ym:s:productID",
            "ym:s:productName",
        ]
    )

    aggregate: dict[tuple[str, ...], dict[str, float]] = defaultdict(
        lambda: {name: 0.0 for name in [*METRIC_COLUMNS, "revenue"]}
    )

    limit = 100000
    offset = 1
    while True:
        body = metrica_get(
            {
                "ids": counter_id,
                "metrics": ",".join(METRICS),
                "dimensions": dimensions,
                "date1": date_from,
                "date2": date_to,
                "attribution": attribution,
                "accuracy": "full",
                "proposed_accuracy": "false",
                "lang": env_optional("METRICA_LANG", "ru"),
                "limit": limit,
                "offset": offset,
            },
            token,
        )
        data = body.get("data", [])
        for item in data:
            dims = [d.get("name") for d in item.get("dimensions", [])]
            values = item.get("metrics", [])
            if len(dims) < 6 or len(values) < len(METRICS):
                continue
            product_name = dim_text(dims[5])
            if not product_name:
                # Строка без товара — это обычный трафик, он уже лежит в marketing_daily_fact.
                continue
            key = (
                clean_text(dims[0]),
                attribution,
                normalize_source(dim_text(dims[1])),
                normalize_campaign_id(dim_text(dims[2])),
                strip_banner(dims[3]),
                dim_text(dims[4]),
                product_name,
            )
            bucket = aggregate[key]
            for index, column in enumerate(METRIC_COLUMNS):
                bucket[column] += parse_int(values[index])
            bucket["revenue"] += money(values[8])

        if len(data) < limit:
            break
        offset += limit

    rows: list[dict[str, Any]] = []
    for key, values in sorted(aggregate.items()):
        row: dict[str, Any] = {
            "date": key[0],
            "attribution": key[1],
            "source": key[2],
            "campaign_id": key[3],
            "ad_id": key[4],
            "product_id": key[5],
            "product_name": key[6],
            "revenue": round(values["revenue"], 2),
        }
        for column in METRIC_COLUMNS:
            row[column] = int(values[column])
        rows.append(row)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Bjorn product funnel to Supabase.")
    add_date_range_args(parser)
    args = parser.parse_args()
    date_from, date_to = resolve_date_range(args)

    attributions = [
        item.strip()
        for item in env_optional("METRICA_ATTRIBUTIONS", ATTRIBUTIONS_DEFAULT).split(",")
        if item.strip()
    ]

    supabase = SupabaseRest()
    # Удаляем окно целиком до записи: строка могла исчезнуть из выборки (товар переименован,
    # источник переклассифицирован), и upsert сам по себе её бы не убрал.
    supabase.delete_date_range(TABLE, date_from, date_to)

    total = 0
    for attribution in attributions:
        rows = fetch_attribution(date_from, date_to, attribution)
        supabase.upsert(TABLE, rows, ON_CONFLICT)
        total += len(rows)
        print(f"[bjorn/products] {attribution}: {len(rows)} строк")
    print(f"[bjorn/products] всего {total} строк за {date_from}..{date_to}")


if __name__ == "__main__":
    main()
