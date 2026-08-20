from __future__ import annotations

import argparse
from collections import defaultdict
from typing import Any

import requests

from .common import (
    SupabaseRest,
    add_date_range_args,
    campaign_name_or_default,
    clean_text,
    env_optional,
    env_required,
    expand_metrica_dimension,
    money,
    normalize_campaign_id,
    normalize_source,
    parse_int,
    resolve_date_range,
)


API_URL = "https://api-metrika.yandex.net/stat/v1/data"


def metrica_get(params: dict[str, Any], token: str) -> dict[str, Any]:
    headers = {"Authorization": f"OAuth {token}"}
    backoff = 2
    for attempt in range(6):
        response = requests.get(API_URL, params=params, headers=headers, timeout=120)
        if response.status_code == 200:
            return response.json()
        if response.status_code in {429, 500, 502, 503, 504} and attempt < 5:
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        raise RuntimeError(f"Metrica API error {response.status_code}: {response.text[:500]}")
    raise RuntimeError("Metrica API retry loop exhausted")


def build_dimensions(attribution: str) -> tuple[str, dict[str, int]]:
    source_dimension = expand_metrica_dimension(
        env_optional("METRICA_SOURCE_DIMENSION", "ym:s:<attribution>TrafficSource"),
        attribution,
    )
    campaign_dimension = expand_metrica_dimension(
        env_optional("METRICA_CAMPAIGN_DIMENSION", "ym:s:<attribution>UTMCampaign"),
        attribution,
    )
    purchase_dim = env_optional("METRICA_ECOM_PURCHASE_DIMENSION", "").strip()
    sku_tpl = env_optional("METRICA_ECOM_SKU_DIMENSION", "").strip()

    parts = ["ym:s:date", source_dimension, campaign_dimension]
    idx: dict[str, int] = {"date": 0, "source": 1, "campaign": 2}
    next_i = 3
    if purchase_dim:
        idx["order"] = next_i
        parts.append(purchase_dim)
        next_i += 1
    if sku_tpl:
        idx["sku"] = next_i
        parts.append(sku_tpl)
        next_i += 1
    idx["product_name"] = next_i
    parts.append("ym:s:productName")

    return ",".join(parts), idx


def fetch_rows(date_from: str, date_to: str) -> list[dict[str, Any]]:
    token = env_required("METRICA_TOKEN")
    counter_id = env_required("METRICA_COUNTER_ID")
    attribution = env_optional("METRICA_ATTRIBUTION", "automatic")
    lang = env_optional("METRICA_LANG", "ru")

    metrics_raw = env_optional(
        "METRICA_ECOM_METRICS",
        "ym:s:productPurchasedQuantity,ym:s:productPurchasedPrice",
    )

    dimensions, dim_idx = build_dimensions(attribution)

    aggregate: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = defaultdict(
        lambda: {"quantity": 0, "revenue": 0.0}
    )

    limit = 100000
    offset = 1
    while True:
        params = {
            "ids": counter_id,
            "metrics": metrics_raw,
            "dimensions": dimensions,
            "date1": date_from,
            "date2": date_to,
            "accuracy": "full",
            "proposed_accuracy": "false",
            "attribution": attribution,
            "lang": lang,
            "limit": limit,
            "offset": offset,
        }
        payload = metrica_get(params, token)
        data = payload.get("data", [])
        if not data:
            break

        for record in data:
            dims = [clean_text(d.get("name", "")) for d in record.get("dimensions", [])]
            mv = record.get("metrics", [])
            row_date = dims[dim_idx["date"]] if dim_idx["date"] < len(dims) else ""
            if not row_date:
                continue

            source = normalize_source(dims[dim_idx["source"]] if dim_idx["source"] < len(dims) else "")
            campaign_raw = dims[dim_idx["campaign"]] if dim_idx["campaign"] < len(dims) else ""
            campaign_id = normalize_campaign_id(campaign_raw)

            order_i = dim_idx.get("order")
            sku_i = dim_idx.get("sku")
            pn_i = dim_idx["product_name"]

            order_id = clean_text(dims[order_i]) if order_i is not None and order_i < len(dims) else ""
            product_sku = clean_text(dims[sku_i]) if sku_i is not None and sku_i < len(dims) else ""
            product_name = clean_text(dims[pn_i]) if pn_i < len(dims) else ""

            qty = parse_int(mv[0] if len(mv) > 0 else 0)
            rev = money(mv[1] if len(mv) > 1 else 0)

            key = (row_date, source, campaign_id, order_id, product_sku, product_name)
            bucket = aggregate[key]
            bucket["quantity"] += qty
            bucket["revenue"] += rev

        if len(data) < limit:
            break
        offset += limit

    rows: list[dict[str, Any]] = []
    for (row_date, source, campaign_id, order_id, product_sku, product_name), vals in sorted(aggregate.items()):
        rows.append(
            {
                "date": row_date,
                "source": source,
                "utm_campaign": campaign_id,
                "campaign_name": campaign_name_or_default(source, campaign_id),
                "order_id": order_id,
                "product_sku": product_sku,
                "product_name": product_name,
                "quantity": int(vals["quantity"]),
                "revenue": round(vals["revenue"], 2),
            }
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Yandex Metrica ecommerce line items to Supabase.")
    add_date_range_args(parser)
    args = parser.parse_args()
    date_from, date_to = resolve_date_range(args)

    rows = fetch_rows(date_from, date_to)
    supabase = SupabaseRest()
    supabase.delete_date_range("marketing_ecom_lines", date_from, date_to)
    supabase.upsert(
        "marketing_ecom_lines",
        rows,
        "date,source,utm_campaign,order_id,product_sku,product_name",
    )


if __name__ == "__main__":
    main()
