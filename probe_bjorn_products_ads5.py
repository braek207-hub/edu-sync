# Проба BJORN, раунд 5 (последний). Закрываю два остатка:
#   1. Шаг «Посмотрели товар»: в stat API его нет среди product*-метрик, но карточки живут
#      на /product/. Проверяю, скрещиваются ли просмотры (ym:pv:*) с источником и с
#      объявлением — если да, ступень встаёт в воронку и по рекламе тоже.
#   2. Реальный объём товарной витрины: сколько строк остаётся, если выкинуть трафик
#      без товарных событий (product_name пуст) — они уже есть в marketing_daily_fact.
# Read-only.
from __future__ import annotations

import os
import time
from typing import Any

import requests

METRIKA_URL = "https://api-metrika.yandex.net/stat/v1/data"
DATE1 = os.environ.get("PROBE_DATE1", "2026-08-01")
DATE2 = os.environ.get("PROBE_DATE2", "2026-08-27")


def metrika(params: dict[str, Any], token: str) -> tuple[int, dict[str, Any]]:
    resp = requests.get(METRIKA_URL, params=params, headers={"Authorization": f"OAuth {token}"}, timeout=180)
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {"raw": resp.text[:300]}


def short_err(body: dict[str, Any]) -> str:
    errs = body.get("errors") or []
    if errs:
        return str(errs[0].get("message", ""))[:170]
    return str(body.get("message", ""))[:170]


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
    print("1. Ступень «Посмотрели товар» = просмотры карточек /product/")
    print("=" * 78)

    print("\n--- 1a. Просмотры карточек в разрезе источника ---")
    status, body = metrika(
        {
            **base,
            "metrics": "ym:pv:pageviews,ym:pv:users",
            "dimensions": "ym:s:lastsignTrafficSource",
            "filters": "ym:pv:URLPathLevel2=='https://bjornlarsen.ru/product/'",
            "limit": 8,
        },
        token,
    )
    print(f"  HTTP {status} строк={body.get('total_rows')} totals={body.get('totals')} {short_err(body) if status != 200 else ''}")
    for d in (body.get("data") or [])[:8]:
        print("   ", [x.get("name") for x in d.get("dimensions", [])], d.get("metrics"))

    print("\n--- 1b. Просмотры карточек в разрезе ОБЪЯВЛЕНИЯ ---")
    status, body = metrika(
        {
            **base,
            "metrics": "ym:pv:pageviews,ym:pv:users",
            "dimensions": "ym:s:lastsignDirectBanner",
            "filters": "ym:pv:URLPathLevel2=='https://bjornlarsen.ru/product/'",
            "limit": 6,
        },
        token,
    )
    print(f"  HTTP {status} строк={body.get('total_rows')} totals={body.get('totals')} {short_err(body) if status != 200 else ''}")
    for d in (body.get("data") or [])[:6]:
        print("   ", [x.get("name") for x in d.get("dimensions", [])], d.get("metrics"))

    print("\n--- 1c. Какой товар: полный URL карточки × дата × источник ---")
    status, body = metrika(
        {
            **base,
            "metrics": "ym:pv:pageviews,ym:pv:users",
            "dimensions": "ym:s:date,ym:s:lastsignTrafficSource,ym:pv:URL",
            "filters": "ym:pv:URLPathLevel2=='https://bjornlarsen.ru/product/'",
            "limit": 6,
        },
        token,
    )
    print(f"  HTTP {status} строк={body.get('total_rows')} {short_err(body) if status != 200 else ''}")
    for d in (body.get("data") or [])[:6]:
        print("   ", [str(x.get("name"))[:70] for x in d.get("dimensions", [])], d.get("metrics"))

    print("\n--- 1d. Тот же срез до объявления (полная платная воронка) ---")
    status, body = metrika(
        {
            **base,
            "metrics": "ym:pv:pageviews,ym:pv:users",
            "dimensions": "ym:s:date,ym:s:lastsignDirectBanner,ym:pv:URL",
            "filters": "ym:pv:URLPathLevel2=='https://bjornlarsen.ru/product/'",
            "limit": 4,
        },
        token,
    )
    print(f"  HTTP {status} строк={body.get('total_rows')} {short_err(body) if status != 200 else ''}")
    for d in (body.get("data") or [])[:4]:
        print("   ", [str(x.get("name"))[:70] for x in d.get("dimensions", [])], d.get("metrics"))

    print("\n" + "=" * 78)
    print("2. Объём товарной витрины без строк-пустышек (есть товарное событие)")
    print("=" * 78)
    metrics = (
        "ym:s:productImpressions,ym:s:productImpressionsUniq,"
        "ym:s:productClicks,ym:s:productClicksUniq,"
        "ym:s:productBasketsQuantity,ym:s:productBasketsUniq,"
        "ym:s:productPurchasedQuantity,ym:s:productPurchasedUniq,ym:s:productPurchasedPrice"
    )
    for filt, label in (
        ("ym:s:productName!n", "productName не пуст"),
        ("", "без фильтра"),
    ):
        params = {
            **base,
            "metrics": metrics,
            "dimensions": "ym:s:date,ym:s:lastsignTrafficSource,ym:s:lastsignUTMCampaign,ym:s:lastsignDirectBanner,ym:s:productID,ym:s:productName",
            "limit": 2,
        }
        if filt:
            params["filters"] = filt
        status, body = metrika(params, token)
        print(f"  {label:<24} HTTP {status} строк={body.get('total_rows')} {short_err(body) if status != 200 else ''}")
        time.sleep(0.5)


if __name__ == "__main__":
    main()
