from __future__ import annotations

import argparse
import time
from collections import defaultdict
from typing import Any

import requests

from .common import (
    SupabaseRest,
    add_date_range_args,
    campaign_name_or_default,
    clean_text,
    derive_source_detail,
    env_optional,
    env_required,
    expand_metrica_dimension,
    money,
    normalize_campaign_id,
    normalize_source,
    parse_int,
    parse_optional_float,
    resolve_date_range,
)


API_URL = "https://api-metrika.yandex.net/stat/v1/data"
ATTRIBUTIONS_DEFAULT = "automatic,first,lastsign"


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


def goal_metric(goal_id: str) -> str:
    return f"ym:s:goal{goal_id}reaches"


def build_metric_defs() -> list[tuple[str, str]]:
    purchase_goal = env_optional("METRICA_GOAL_PURCHASE") or env_optional("METRICA_GOAL_ID")
    add_to_cart_goal = env_optional("METRICA_GOAL_ADD_TO_CART")
    begin_checkout_goal = env_optional("METRICA_GOAL_BEGIN_CHECKOUT")

    metric_defs: list[tuple[str, str]] = [("visits", "ym:s:visits")]
    if add_to_cart_goal:
        metric_defs.append(("add_to_cart", goal_metric(add_to_cart_goal)))
    if begin_checkout_goal:
        metric_defs.append(("begin_checkout", goal_metric(begin_checkout_goal)))
    if purchase_goal:
        metric_defs.append(("purchases", goal_metric(purchase_goal)))
    metric_defs.append(("revenue", "ym:s:ecommerceRevenue"))
    metric_defs.append(("bounce_rate", "ym:s:bounceRate"))
    metric_defs.append(("page_depth", "ym:s:pageDepth"))
    return metric_defs


def fetch_rows_for_attribution(date_from: str, date_to: str, attribution: str) -> list[dict[str, Any]]:
    token = env_required("METRICA_TOKEN")
    counter_id = env_required("METRICA_COUNTER_ID")
    lang = env_optional("METRICA_LANG", "ru")
    source_dimension = expand_metrica_dimension(
        env_optional("METRICA_SOURCE_DIMENSION", "ym:s:<attribution>TrafficSource"),
        attribution,
    )
    source_engine_dimension = expand_metrica_dimension(
        env_optional("METRICA_SOURCE_ENGINE_DIMENSION", "ym:s:<attribution>SourceEngine"),
        attribution,
    )
    utm_source_dimension = expand_metrica_dimension(
        env_optional("METRICA_UTM_SOURCE_DIMENSION", "ym:s:<attribution>UTMSource"),
        attribution,
    )
    campaign_dimension = expand_metrica_dimension(
        env_optional("METRICA_CAMPAIGN_DIMENSION", "ym:s:<attribution>UTMCampaign"),
        attribution,
    )

    metric_defs = build_metric_defs()
    metrics = ",".join(metric for _, metric in metric_defs)
    # dims: [0]=date [1]=TrafficSource [2]=SourceEngine(детально) [3]=UTMSource [4]=UTMCampaign
    # SourceEngine = Metrica's native "Источник трафика (детально)" — covers search/social/
    #   messengers/recommendations/paid in one dimension. UTMSource fallback for email/CRM.
    dimensions = ",".join([
        "ym:s:date", source_dimension, source_engine_dimension,
        utm_source_dimension, campaign_dimension,
    ])

    aggregate: dict[tuple[str, str, str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "visits": 0,
            "add_to_cart": 0,
            "begin_checkout": 0,
            "purchases": 0,
            "revenue": 0.0,
            "bounce_weight": 0.0,
            "depth_weight": 0.0,
            "bounce_visits": 0,
            "depth_visits": 0,
        }
    )

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
            "lang": lang,
            "limit": limit,
            "offset": offset,
        }
        payload = metrica_get(params, token)
        data = payload.get("data", [])
        if not data:
            break

        for record in data:
            visits_row = 0
            dims = [clean_text(d.get("name", "")) for d in record.get("dimensions", [])]
            metrics_values = record.get("metrics", [])
            row_date = dims[0] if len(dims) > 0 else ""
            if not row_date:
                continue
            source        = normalize_source(dims[1] if len(dims) > 1 else "")
            source_eng    = dims[2] if len(dims) > 2 else ""
            utm_src_raw   = dims[3] if len(dims) > 3 else ""
            campaign_id   = normalize_campaign_id(dims[4] if len(dims) > 4 else "")
            source_detail = derive_source_detail(source_eng, utm_src_raw, source)
            item = aggregate[(row_date, source, source_detail, campaign_id)]

            visits_row = 0
            for idx, (name, _) in enumerate(metric_defs):
                value = metrics_values[idx] if idx < len(metrics_values) else 0
                if name == "visits":
                    visits_row = parse_int(value)
                    item["visits"] += visits_row
                elif name == "revenue":
                    item[name] += money(value)
                elif name == "bounce_rate":
                    bp = parse_optional_float(value)
                    if visits_row > 0 and bp is not None:
                        item["bounce_weight"] += bp * visits_row
                        item["bounce_visits"] += visits_row
                elif name == "page_depth":
                    dp = parse_optional_float(value)
                    if visits_row > 0 and dp is not None:
                        item["depth_weight"] += dp * visits_row
                        item["depth_visits"] += visits_row
                else:
                    item[name] += parse_int(value)

        if len(data) < limit:
            break
        offset += limit

    _max_daily_rev = float(env_optional("METRICA_MAX_DAILY_REVENUE", "10000000"))
    for _key, _item in aggregate.items():
        if _item.get("revenue", 0) > _max_daily_rev:
            import warnings as _warnings
            _warnings.warn(
                f"Anomalous revenue {_item['revenue']:.0f} for {_key}. "
                f"Possible cumulative data from Metrica API. "
                f"Set METRICA_MAX_DAILY_REVENUE env var to override threshold.",
                RuntimeWarning,
                stacklevel=2,
            )

    rows: list[dict[str, Any]] = []
    for (row_date, source, source_detail, campaign_id), values in sorted(aggregate.items()):
        bv = int(values["bounce_visits"])
        dv = int(values["depth_visits"])
        bounce_pct = round(values["bounce_weight"] / bv, 2) if bv else None
        depth_avg = round(values["depth_weight"] / dv, 4) if dv else None
        rows.append(
            {
                "date": row_date,
                "attribution": attribution,
                "source": source,
                "source_detail": source_detail,
                "utm_campaign": campaign_id,
                "campaign_name": campaign_name_or_default(source, campaign_id),
                "visits": values["visits"],
                "add_to_cart": values["add_to_cart"],
                "begin_checkout": values["begin_checkout"],
                "purchases": values["purchases"],
                "revenue": round(values["revenue"], 2),
                "bounce_rate": bounce_pct,
                "page_depth": depth_avg,
            }
        )
    return rows


def fetch_rows(date_from: str, date_to: str) -> list[dict[str, Any]]:
    attributions_env = env_optional("METRICA_ATTRIBUTIONS", ATTRIBUTIONS_DEFAULT)
    attributions = [a.strip() for a in attributions_env.split(",") if a.strip()]
    all_rows: list[dict[str, Any]] = []
    for attribution in attributions:
        rows = fetch_rows_for_attribution(date_from, date_to, attribution)
        all_rows.extend(rows)
        print(f"  stg_metrica attribution={attribution}: {len(rows)} rows")
    return all_rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Yandex Metrica daily campaign data to Supabase staging.")
    add_date_range_args(parser)
    args = parser.parse_args()
    date_from, date_to = resolve_date_range(args)

    rows = fetch_rows(date_from, date_to)
    supabase = SupabaseRest()
    supabase.delete_date_range("stg_metrica_daily_campaign", date_from, date_to)
    supabase.upsert("stg_metrica_daily_campaign", rows, "date,attribution,source,source_detail,utm_campaign")


if __name__ == "__main__":
    main()
