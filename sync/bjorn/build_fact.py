from __future__ import annotations

import argparse
from typing import Any

from .common import (
    AD_SOURCE_NAME,
    SupabaseRest,
    add_date_range_args,
    campaign_name_or_default,
    clean_text,
    fact_campaign_id,
    infer_source_type,
    is_paid_source,
    money,
    parse_int,
    parse_optional_float,
    resolve_date_range,
)


def _norm_campaign_name(name: str) -> str:
    return clean_text(name).lower()


def _is_number_only(s: str) -> bool:
    """Returns True if s contains only digits (and optional hyphens) — no descriptive text."""
    stripped = s.strip().lstrip("-")
    return bool(stripped) and stripped.isdigit()


def build_fact_rows(metrica_rows: list[dict[str, Any]], direct_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    direct_by_key = {
        (str(row["date"]), str(row["campaign_id"])): row
        for row in direct_rows
        if row.get("date") and row.get("campaign_id")
    }
    # Secondary index by campaign name — matches Metrica rows that use text names in utm_campaign
    direct_by_name = {
        (str(row["date"]), _norm_campaign_name(str(row.get("campaign_name") or ""))): row
        for row in direct_rows
        if row.get("date") and row.get("campaign_name")
    }
    # Track which direct keys are consumed under "automatic" attribution to avoid
    # triple-counting unmatched Direct-only rows across attributions.
    consumed_direct_keys: set[tuple[str, str]] = set()
    # Track (date, attribution, campaign_id) where Direct cost was already applied to
    # avoid double-counting when same campaign has multiple source_detail rows.
    direct_cost_applied: set[tuple[str, str, str]] = set()
    fact_rows: list[dict[str, Any]] = []
    # Track (date, attribution, source, source_detail, campaign_id) → index for geo-variant merge
    fact_index: dict[tuple[str, str, str, str, str], int] = {}

    for row in metrica_rows:
        row_date = str(row["date"])
        attribution = str(row.get("attribution") or "automatic")
        source = str(row.get("source") or "organic")
        source_detail = str(row.get("source_detail") or "")
        metrica_campaign_id = str(row.get("utm_campaign") or "")
        campaign_id = fact_campaign_id(source, metrica_campaign_id)
        direct_key = (row_date, campaign_id)
        direct_row = None
        if is_paid_source(source) and campaign_id:
            direct_row = direct_by_key.get(direct_key)
            # Secondary lookup: if utm_campaign is a text name (not a number), try matching by name
            if not direct_row and not _is_number_only(metrica_campaign_id) and metrica_campaign_id:
                name_key = (row_date, _norm_campaign_name(metrica_campaign_id))
                direct_row = direct_by_name.get(name_key)
        if direct_row and attribution == "automatic":
            consumed_direct_keys.add(direct_key)
            # For name-based matches the Direct row's numeric ID differs from the Metrica text key.
            # Mark the Direct row's own (date, campaign_id) as consumed to prevent the direct-only
            # loop from re-adding it and double-counting impressions/clicks/cost.
            direct_actual_key = (row_date, str(direct_row.get("campaign_id") or ""))
            if direct_actual_key != direct_key:
                consumed_direct_keys.add(direct_actual_key)
        # Apply Direct cost (impressions/clicks/cost) only once per campaign per day.
        # Same campaign_id can appear in multiple rows with different source_detail.
        direct_cost_key = (row_date, attribution, campaign_id)
        if direct_row and direct_cost_key in direct_cost_applied:
            direct_row = None
        elif direct_row:
            direct_cost_applied.add(direct_cost_key)

        direct_name = str(direct_row.get("campaign_name") or "") if direct_row else ""
        metrica_name = str(row.get("campaign_name") or "")
        campaign_name = campaign_name_or_default(source, campaign_id, direct_name or metrica_name)
        # Number-only campaign_id with no Direct match → collapse into nameless bucket
        # (shows as "Не определено" in table rather than an unreadable numeric ID)
        if _is_number_only(campaign_id) and not direct_row:
            campaign_id = ""
            campaign_name = ""
        source_type = str(direct_row.get("source_type") or "") if direct_row else ""
        if is_paid_source(source) and not source_type:
            source_type = infer_source_type(campaign_name)

        br_m = parse_optional_float(row.get("bounce_rate"))
        pd_m = parse_optional_float(row.get("page_depth"))

        fact_key = (row_date, attribution, source, source_detail, campaign_id)
        if fact_key in fact_index:
            # Merge geo-variant into existing row (sum additive metrics)
            ex = fact_rows[fact_index[fact_key]]
            ex["visits"]         += parse_int(row.get("visits"))
            ex["add_to_cart"]    += parse_int(row.get("add_to_cart"))
            ex["begin_checkout"] += parse_int(row.get("begin_checkout"))
            ex["purchases"]      += parse_int(row.get("purchases"))
            ex["revenue"]        = round(ex["revenue"] + money(row.get("revenue")), 2)
            # Weighted bounce_rate / page_depth
            v_new = parse_int(row.get("visits"))
            if v_new > 0 and br_m is not None:
                v_ex = ex["visits"] - v_new
                br_ex = ex.get("bounce_rate") or 0.0
                ex["bounce_rate"] = (br_ex * v_ex + br_m * v_new) / ex["visits"] if ex["visits"] else None
            if v_new > 0 and pd_m is not None:
                v_ex = ex["visits"] - v_new
                pd_ex = ex.get("page_depth") or 0.0
                ex["page_depth"] = (pd_ex * v_ex + pd_m * v_new) / ex["visits"] if ex["visits"] else None
        else:
            fact_index[fact_key] = len(fact_rows)
            fact_rows.append(
                {
                    "date": row_date,
                    "attribution": attribution,
                    "source": source,
                    "source_detail": source_detail,
                    "campaign_id": campaign_id,
                    "campaign_name": campaign_name,
                    "source_type": source_type,
                    "visits": parse_int(row.get("visits")),
                    "add_to_cart": parse_int(row.get("add_to_cart")),
                    "begin_checkout": parse_int(row.get("begin_checkout")),
                    "purchases": parse_int(row.get("purchases")),
                    "revenue": money(row.get("revenue")),
                    "impressions": parse_int(direct_row.get("impressions")) if direct_row else 0,
                    "clicks": parse_int(direct_row.get("clicks")) if direct_row else 0,
                    "cost_vat": money(direct_row.get("cost_vat")) if direct_row else 0.0,
                    "bounce_rate": br_m,
                    "page_depth": pd_m,
                }
            )

    # Direct-only rows (no Metrica match) — added once under "automatic" attribution
    for key, direct_row in sorted(direct_by_key.items()):
        if key in consumed_direct_keys:
            continue
        campaign_name = str(direct_row.get("campaign_name") or direct_row.get("campaign_id") or "")
        source_type = str(direct_row.get("source_type") or "") or infer_source_type(campaign_name)
        fact_rows.append(
            {
                "date": str(direct_row["date"]),
                "attribution": "automatic",
                "source": AD_SOURCE_NAME,
                "source_detail": "Яндекс.Директ",
                "campaign_id": str(direct_row["campaign_id"]),
                "campaign_name": campaign_name,
                "source_type": source_type,
                "visits": 0,
                "add_to_cart": 0,
                "begin_checkout": 0,
                "purchases": 0,
                "revenue": 0.0,
                "impressions": parse_int(direct_row.get("impressions")),
                "clicks": parse_int(direct_row.get("clicks")),
                "cost_vat": money(direct_row.get("cost_vat")),
                "bounce_rate": None,
                "page_depth": None,
            }
        )

    return sorted(fact_rows, key=lambda item: (item["date"], item["attribution"], item["source"], item["campaign_id"]))


def main() -> None:
    parser = argparse.ArgumentParser(description="Build marketing_daily_fact from Metrica and Direct staging tables.")
    add_date_range_args(parser)
    args = parser.parse_args()
    date_from, date_to = resolve_date_range(args)

    supabase = SupabaseRest()
    metrica_rows = supabase.select_date_range("stg_metrica_daily_campaign", date_from, date_to)
    direct_rows = supabase.select_date_range("stg_direct_daily_campaign", date_from, date_to)
    fact_rows = build_fact_rows(metrica_rows, direct_rows)

    supabase.delete_date_range("marketing_daily_fact", date_from, date_to)
    supabase.upsert("marketing_daily_fact", fact_rows, "date,attribution,source,source_detail,campaign_id")


if __name__ == "__main__":
    main()
