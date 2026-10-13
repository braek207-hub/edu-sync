# Проба BJORN (только чтение): можно ли собрать покупки Метрики ПОШТУЧНО (purchaseID) в каждой
# модели атрибуции так, чтобы по ключу витрины (дата, источник, детально, кампания) их число
# совпало с покупками stg_metrica_daily_campaign. Это условие клика «ПОКУПКИ» → список.
# Плюс: отдаёт ли Метрика состав покупки (purchaseID × productName).
# Репозиторий публичный — печатаются только агрегаты, без номеров заказов.
from __future__ import annotations

import os
from collections import Counter, defaultdict

from sync.bjorn.common import (
    clean_text,
    derive_source_detail,
    env_required,
    metrica_stat_rows,
    money,
    normalize_campaign_id,
    normalize_source,
    parse_int,
)
from sync.bjorn.sync_metrica import fetch_rows_for_attribution

D1 = os.environ.get("PROBE_DATE1") or "2026-09-01"
D2 = os.environ.get("PROBE_DATE2") or "2026-09-30"
SKIP = {"", "(not set)", "0", "--"}


def purchase_rows(attribution: str) -> list[dict]:
    token = env_required("METRICA_TOKEN")
    dims = [
        "ym:s:purchaseID",
        "ym:s:date",
        f"ym:s:{attribution}TrafficSource",
        f"ym:s:{attribution}SourceEngine",
        f"ym:s:{attribution}UTMSource",
        f"ym:s:{attribution}UTMCampaign",
    ]
    params = {
        "ids": env_required("METRICA_COUNTER_ID"),
        "metrics": "ym:s:ecommercePurchases,ym:s:ecommerceRevenue",
        "dimensions": ",".join(dims),
        "date1": D1,
        "date2": D2,
        "attribution": attribution,
        "accuracy": "full",
        "proposed_accuracy": "false",
        "lang": "ru",
    }
    out = []
    for rec in metrica_stat_rows(params, token, timeout=180, page_limit=10000):
        d = [clean_text(x.get("name", "")) for x in rec.get("dimensions", [])]
        m = rec.get("metrics", [])
        source = normalize_source(d[2])
        out.append(
            {
                "order_id": d[0],
                "key": (d[1], source, derive_source_detail(d[3], d[4], source), normalize_campaign_id(d[5])),
                "purchases": parse_int(m[0]),
                "revenue": money(m[1]),
            }
        )
    return out


def main() -> None:
    print(f"окно {D1}..{D2}")
    for attribution in ("automatic", "first", "lastsign"):
        stg = fetch_rows_for_attribution(D1, D2, attribution)
        stg_p: Counter = Counter()
        stg_r: dict = defaultdict(float)
        for r in stg:
            k = (r["date"], r["source"], r["source_detail"], r["utm_campaign"])
            stg_p[k] += r["purchases"]
            stg_r[k] += r["revenue"]

        rows = purchase_rows(attribution)
        valid = [r for r in rows if r["order_id"] not in SKIP and r["purchases"] > 0]
        per_order = Counter(r["order_id"] for r in valid)
        pp: Counter = Counter()
        pr: dict = defaultdict(float)
        for r in valid:
            pp[r["key"]] += r["purchases"]
            pr[r["key"]] += r["revenue"]
        skipped = [r for r in rows if r["order_id"] in SKIP]

        keys = set(stg_p) | set(pp)
        diff_keys = [k for k in keys if stg_p[k] != pp[k]]
        print(
            f"\n[{attribution}] stg: покупок {sum(stg_p.values())}, выручка {round(sum(stg_r.values()))}"
            f" | поштучно: строк {len(rows)}, заказов {len(per_order)}, покупок {sum(pp.values())},"
            f" выручка {round(sum(pr.values()))}"
        )
        print(
            f"  заказ в >1 строке: {sum(1 for v in per_order.values() if v > 1)};"
            f" строк без purchaseID: {len(skipped)} (покупок {sum(r['purchases'] for r in skipped)})"
        )
        print(f"  ключей витрины с покупками: {sum(1 for k in keys if stg_p[k] or pp[k])}, расходятся: {len(diff_keys)}")
        for k in sorted(diff_keys)[:15]:
            print(f"    {k[0]} | {k[1]} | {k[2]} | {k[3] or '—'}: stg {stg_p[k]} vs поштучно {pp[k]}")

    # Состав: purchaseID × товар (без кампаний — только факт, что Метрика отдаёт позиции).
    token = env_required("METRICA_TOKEN")
    params = {
        "ids": env_required("METRICA_COUNTER_ID"),
        "metrics": "ym:s:productPurchasedQuantity,ym:s:productPurchasedPrice",
        "dimensions": "ym:s:purchaseID,ym:s:date,ym:s:productName",
        "date1": D1,
        "date2": D2,
        "accuracy": "full",
        "proposed_accuracy": "false",
        "lang": "ru",
    }
    items = metrica_stat_rows(params, token, timeout=180, page_limit=10000)
    with_id = [r for r in items if clean_text(r["dimensions"][0].get("name", "")) not in SKIP]
    orders = {clean_text(r["dimensions"][0].get("name", "")) for r in with_id}
    print(f"\n[состав] строк {len(items)}, с purchaseID {len(with_id)}, заказов с позициями {len(orders)}")


if __name__ == "__main__":
    main()
