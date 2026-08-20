from __future__ import annotations

import argparse
from typing import Any

from .common import SupabaseRest, add_date_range_args, money, parse_int, resolve_date_range


def sum_int(rows: list[dict[str, Any]], key: str) -> int:
    return sum(parse_int(row.get(key)) for row in rows)


def sum_money(rows: list[dict[str, Any]], key: str) -> float:
    return round(sum(money(row.get(key)) for row in rows), 2)


def assert_equal(label: str, left: int | float, right: int | float, rel_tol: float = 0.0) -> None:
    if isinstance(left, float) or isinstance(right, float):
        l, r = float(left), float(right)
        if rel_tol > 0:
            denom = max(abs(l), abs(r), 1.0)
            ok = abs(l - r) / denom < rel_tol
        else:
            ok = abs(l - r) < 0.01
    else:
        ok = left == right
    print(f"{label}: staging={left} fact={right}")
    if not ok:
        raise RuntimeError(f"{label} mismatch: staging={left}, fact={right}")


def weighted_visits_metric(rows: list[dict[str, Any]], visits_key: str, rate_key: str) -> float:
    total = 0.0
    for row in rows:
        visits = parse_int(row.get(visits_key))
        raw_rate = row.get(rate_key)
        if visits <= 0 or raw_rate is None:
            continue
        total += visits * float(raw_rate)
    return round(total, 4)


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify marketing_daily_fact totals against staging tables.")
    add_date_range_args(parser)
    args = parser.parse_args()
    date_from, date_to = resolve_date_range(args)

    supabase = SupabaseRest()
    metrica_all = supabase.select_date_range("stg_metrica_daily_campaign", date_from, date_to)
    direct = supabase.select_date_range("stg_direct_daily_campaign", date_from, date_to)
    fact_all = supabase.select_date_range("marketing_daily_fact", date_from, date_to)

    # Metrica/fact metrics are attribution-specific — compare per attribution
    attributions = sorted({str(r.get("attribution") or "automatic") for r in metrica_all})
    for attr in attributions:
        metrica = [r for r in metrica_all if str(r.get("attribution") or "automatic") == attr]
        fact = [r for r in fact_all if str(r.get("attribution") or "automatic") == attr]
        print(f"\n--- attribution={attr} ({len(metrica)} staging, {len(fact)} fact rows) ---")
        assert_equal("visits", sum_int(metrica, "visits"), sum_int(fact, "visits"))
        assert_equal("add_to_cart", sum_int(metrica, "add_to_cart"), sum_int(fact, "add_to_cart"))
        assert_equal("begin_checkout", sum_int(metrica, "begin_checkout"), sum_int(fact, "begin_checkout"))
        assert_equal("purchases", sum_int(metrica, "purchases"), sum_int(fact, "purchases"))
        assert_equal("revenue", sum_money(metrica, "revenue"), sum_money(fact, "revenue"))
        wb_m = weighted_visits_metric(metrica, "visits", "bounce_rate")
        wb_f = weighted_visits_metric(fact, "visits", "bounce_rate")
        assert_equal("bounce_rate weighted sum(rate*visits)", wb_m, wb_f, rel_tol=0.005)
        wd_m = weighted_visits_metric(metrica, "visits", "page_depth")
        wd_f = weighted_visits_metric(fact, "visits", "page_depth")
        assert_equal("page_depth weighted sum(depth*visits)", wd_m, wd_f, rel_tol=0.005)

    # Direct cost lives once in fact (under "automatic") — compare against staging Direct
    fact_auto = [r for r in fact_all if str(r.get("attribution") or "automatic") == "automatic"]
    print("\n--- Direct cost (attribution=automatic) ---")
    assert_equal("impressions", sum_int(direct, "impressions"), sum_int(fact_auto, "impressions"))
    assert_equal("clicks", sum_int(direct, "clicks"), sum_int(fact_auto, "clicks"))
    assert_equal("cost_vat", sum_money(direct, "cost_vat"), sum_money(fact_auto, "cost_vat"))

    fact_keys = [
        (row.get("date"), row.get("attribution"), row.get("source"), row.get("source_detail"), row.get("campaign_id"))
        for row in fact_all
    ]
    if len(fact_keys) != len(set(fact_keys)):
        raise RuntimeError("marketing_daily_fact contains duplicate primary keys in selected range")
    print(f"\nVerified {len(fact_all)} fact rows for {date_from}..{date_to}")


if __name__ == "__main__":
    main()
