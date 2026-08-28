# Проба BJORN, раунд 4. Раунд 3 нашёл ym:s:productClicks(Uniq) — «кликнули по товару», но
# шаг «Посмотрели товар» (карточка) в stat/v1/data не отдаётся ни в одной форме имени.
# Здесь закрываю два вопроса:
#   1. Есть ли обходной путь к шагу «просмотр карточки» — просмотры URL карточек товара
#      (ym:pv:URL / ym:s:URLPath) и пресеты ecommerce.
#   2. Объём ЕДИНОЙ витрины: date × источник × кампания × объявление × товар — влезает ли
#      всё в одну таблицу вместо двух (у рекламы ad_id заполнен, у органики NULL).
# Read-only.
from __future__ import annotations

import os
import time
from typing import Any

import requests

METRIKA_URL = "https://api-metrika.yandex.net/stat/v1/data"
DATE1 = os.environ.get("PROBE_DATE1", "2026-08-01")
DATE2 = os.environ.get("PROBE_DATE2", "2026-08-27")


def metrika(params: dict[str, Any], token: str, path: str = "") -> tuple[int, dict[str, Any]]:
    resp = requests.get(METRIKA_URL + path, params=params, headers={"Authorization": f"OAuth {token}"}, timeout=180)
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {"raw": resp.text[:300]}


def short_err(body: dict[str, Any]) -> str:
    errs = body.get("errors") or []
    if errs:
        return str(errs[0].get("message", ""))[:150]
    return str(body.get("message", ""))[:150]


def main() -> None:
    token = os.environ["METRICA_TOKEN"]
    counter = os.environ["METRICA_COUNTER_ID"]
    base = {
        "ids": counter,
        "date1": DATE1,
        "date2": DATE2,
        "accuracy": "full",
        "proposed_accuracy": "false",
        "attribution": "lastsign",
        "lang": "ru",
    }

    print("=" * 78)
    print(f"1. Обходной путь к шагу «Посмотрели товар» · {DATE1}..{DATE2}")
    print("=" * 78)

    print("\n--- 1a. Просмотры URL: как выглядят адреса карточек товара ---")
    status, body = metrika({**base, "metrics": "ym:pv:pageviews,ym:pv:users", "dimensions": "ym:pv:URLPathLevel1", "limit": 12}, token)
    print(f"  URLPathLevel1 HTTP {status} строк={body.get('total_rows')} {short_err(body) if status != 200 else ''}")
    for d in (body.get("data") or [])[:12]:
        print("   ", [x.get("name") for x in d.get("dimensions", [])], d.get("metrics"))

    print("\n--- 1b. Второй уровень пути (каталог/карточка) ---")
    status, body = metrika({**base, "metrics": "ym:pv:pageviews,ym:pv:users", "dimensions": "ym:pv:URLPathLevel2", "limit": 10}, token)
    print(f"  URLPathLevel2 HTTP {status} строк={body.get('total_rows')}")
    for d in (body.get("data") or [])[:10]:
        print("   ", [x.get("name") for x in d.get("dimensions", [])], d.get("metrics"))

    print("\n--- 1c. Пресеты ecommerce в stat API ---")
    for preset in ("ecommerce_orders", "ecommerce_purchases", "ecommerce_funnel", "ecommerce_products", "products"):
        status, body = metrika({**base, "preset": preset, "limit": 1}, token)
        if status == 200:
            print(f"  OK   preset={preset:<22} query={body.get('query', {}).get('metrics')}")
        else:
            print(f"  {status}  preset={preset:<22} {short_err(body)}")
        time.sleep(0.3)

    print("\n" + "=" * 78)
    print("2. Объём единой витрины: date × источник × кампания × объявление × товар")
    print("=" * 78)
    metrics = (
        "ym:s:visits,ym:s:users,"
        "ym:s:productImpressions,ym:s:productImpressionsUniq,"
        "ym:s:productClicks,ym:s:productClicksUniq,"
        "ym:s:productBasketsQuantity,ym:s:productBasketsUniq,"
        "ym:s:productPurchasedQuantity,ym:s:productPurchasedUniq,ym:s:productPurchasedPrice"
    )
    for dims, label in (
        ("ym:s:date,ym:s:lastsignTrafficSource,ym:s:lastsignUTMCampaign,ym:s:lastsignDirectBanner,ym:s:productID,ym:s:productName", "полная (с ad_id и productID)"),
        ("ym:s:date,ym:s:lastsignTrafficSource,ym:s:lastsignUTMCampaign,ym:s:lastsignDirectBanner,ym:s:productName", "без productID"),
        ("ym:s:date,ym:s:lastsignTrafficSource,ym:s:lastsignUTMCampaign,ym:s:productName", "без объявления"),
    ):
        status, body = metrika({**base, "metrics": metrics, "dimensions": dims, "limit": 2}, token)
        print(f"  {label:<30} HTTP {status} строк={body.get('total_rows')} {short_err(body) if status != 200 else ''}")
        for d in (body.get("data") or [])[:2]:
            print("     ", [x.get("name") for x in d.get("dimensions", [])], d.get("metrics"))
        time.sleep(0.5)

    print("\n--- 2b. Витрина объявлений без товара: date × объявление ---")
    status, body = metrika(
        {
            **base,
            "metrics": "ym:s:visits,ym:s:users,ym:s:bounceRate,ym:s:pageDepth," + metrics.split("ym:s:productImpressions")[0].rstrip(",") + ",ym:s:productBasketsUniq,ym:s:productPurchasedQuantity,ym:s:productPurchasedPrice",
            "dimensions": "ym:s:date,ym:s:lastsignDirectBanner",
            "limit": 2,
        },
        token,
    )
    print(f"  HTTP {status} строк={body.get('total_rows')} {short_err(body) if status != 200 else ''}")


if __name__ == "__main__":
    main()
