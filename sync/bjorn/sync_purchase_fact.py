# BJORN: покупки Метрики поштучно (purchaseID) в каждой модели атрибуции → bjorn_metrika_purchase_fact.
#
# Зачем: клик по «ПОКУПКИ» в Panda-BI открывает список покупок ячейки, а сама ячейка
# складывается из этих же строк (lib/dashboard/handlers/bjorn.ts) — число и список не могут
# разойтись. Поэтому ключ строки — ключ витрины: измерения те же, что у sync_metrica (дата,
# источник, детально, UTM-кампания), а кампания решается тем же кодом build_fact, что строит
# marketing_daily_fact (match_direct + resolve_fact_campaign).
#
# Проба 13.10.2026 (probe_bjorn_purchase_attr.py, сентябрь): во всех трёх моделях поштучные
# покупки по каждому ключу = покупки витрины (101/101, выручка до рубля), каждая покупка —
# одна строка. Состав по purchaseID Stat API не отдаёт (4024: два множества в группировках) —
# Panda берёт его из bjorn_order_items Битрикса.
#
# bjorn_metrika_purchases (sync_purchases.py) не заменяется: там lastsign с utm_medium для
# склейки CRM-заказов, здесь — модели атрибуции для ячейки Метрики.
from __future__ import annotations

import argparse
from typing import Any

from .build_fact import direct_index, match_direct, resolve_fact_campaign
from .common import (
    SupabaseRest,
    add_date_range_args,
    campaign_name_or_default,
    clean_text,
    derive_source_detail,
    env_optional,
    env_required,
    expand_metrica_dimension,
    metrica_stat_rows,
    money,
    normalize_campaign_id,
    normalize_source,
    parse_int,
    resolve_date_range,
)
from .sync_metrica import ATTRIBUTIONS_DEFAULT

TABLE = "bjorn_metrika_purchase_fact"
SKIP_ORDER_IDS = {"", "(not set)", "0", "--"}


def fetch_purchases(date_from: str, date_to: str, attribution: str) -> list[dict[str, Any]]:
    """Покупки модели: по строке на purchaseID с ключом stg_metrica_daily_campaign."""
    token = env_required("METRICA_TOKEN")
    dims = [
        "ym:s:purchaseID",
        "ym:s:date",
        expand_metrica_dimension("ym:s:<attribution>TrafficSource", attribution),
        expand_metrica_dimension("ym:s:<attribution>SourceEngine", attribution),
        expand_metrica_dimension("ym:s:<attribution>UTMSource", attribution),
        expand_metrica_dimension("ym:s:<attribution>UTMCampaign", attribution),
    ]
    params = {
        "ids": env_required("METRICA_COUNTER_ID"),
        "metrics": "ym:s:ecommercePurchases,ym:s:ecommerceRevenue",
        "dimensions": ",".join(dims),
        "date1": date_from,
        "date2": date_to,
        "attribution": attribution,
        "accuracy": "full",
        "proposed_accuracy": "false",
        "lang": env_optional("METRICA_LANG", "ru"),
    }
    rows: list[dict[str, Any]] = []
    for record in metrica_stat_rows(params, token, timeout=180, page_limit=10000):
        d = [clean_text(x.get("name", "")) for x in record.get("dimensions", [])]
        m = record.get("metrics", [])
        source = normalize_source(d[2])
        campaign = normalize_campaign_id(d[5])
        rows.append(
            {
                "order_id": d[0],
                "date": d[1],
                "attribution": attribution,
                "source": source,
                "source_detail": derive_source_detail(d[3], d[4], source),
                "utm_campaign": campaign,
                "campaign_name": campaign_name_or_default(source, campaign),
                "purchases": parse_int(m[0] if m else 0),
                "revenue": money(m[1] if len(m) > 1 else 0),
            }
        )
    return rows


def build_purchase_fact_rows(
    purchase_rows: list[dict[str, Any]], direct_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    index = direct_index(direct_rows)
    by_order: dict[tuple[str, str], dict[str, Any]] = {}
    for row in purchase_rows:
        order_id = str(row["order_id"]).strip()
        if order_id in SKIP_ORDER_IDS or row["purchases"] <= 0:
            continue
        row_date = str(row["date"])
        source = str(row["source"] or "organic")
        campaign_id, direct_row = match_direct(row_date, source, str(row["utm_campaign"] or ""), index)
        campaign_id, campaign_name, source_type = resolve_fact_campaign(
            source, campaign_id, str(row["campaign_name"] or ""), direct_row
        )
        fact = {
            "attribution": row["attribution"],
            "order_id": order_id,
            "date": row_date,
            "source": source,
            "source_detail": str(row["source_detail"] or ""),
            "campaign_id": campaign_id,
            "campaign_name": campaign_name,
            "source_type": source_type,
            "utm_campaign": str(row["utm_campaign"] or ""),
            "purchases": row["purchases"],
            "revenue": round(row["revenue"], 2),
        }
        # Покупка в двух визитах (проба: ни одной) — одна строка, самая ранняя. Число ячейки
        # тогда меньше витрины, и Panda докладывает разницу на строку кампании без номера.
        key = (fact["attribution"], order_id)
        kept = by_order.get(key)
        if kept is None or row_date < kept["date"]:
            by_order[key] = fact
    return sorted(by_order.values(), key=lambda r: (r["date"], r["attribution"], r["order_id"]))


def main() -> None:
    parser = argparse.ArgumentParser(description="BJORN: покупки Метрики поштучно по моделям атрибуции")
    add_date_range_args(parser)
    date_from, date_to = resolve_date_range(parser.parse_args())

    attributions = [
        a.strip() for a in env_optional("METRICA_ATTRIBUTIONS", ATTRIBUTIONS_DEFAULT).split(",") if a.strip()
    ]
    purchases: list[dict[str, Any]] = []
    for attribution in attributions:
        rows = fetch_purchases(date_from, date_to, attribution)
        print(f"  {TABLE} attribution={attribution}: {len(rows)} строк")
        purchases.extend(rows)

    supabase = SupabaseRest()
    direct_rows = supabase.select_date_range("stg_direct_daily_campaign", date_from, date_to)
    fact_rows = build_purchase_fact_rows(purchases, direct_rows)
    print(f"{TABLE}: {date_from}..{date_to} → {len(fact_rows)} покупок")

    supabase.delete_date_range(TABLE, date_from, date_to)
    supabase.upsert(TABLE, fact_rows, on_conflict="attribution,order_id")


if __name__ == "__main__":
    main()
