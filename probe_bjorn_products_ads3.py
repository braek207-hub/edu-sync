# Проба BJORN, раунд 3. В интерфейсе Метрики отчёт «Ecommerce-воронка» показывает шесть
# ступеней: Посетители → Посмотрели товар в списке → Кликнули по товару → Посмотрели товар →
# Добавили в корзину → Совершили заказ. Раунд 1 нашёл только impressions/baskets/purchased —
# значит имена метрик «клик по товару» и «просмотр карточки» я угадал неверно.
# Здесь перебираю формы имён и проверяю, что найденное скрещивается с товаром, источником
# и объявлением (ym:s:<attr>DirectBanner). Read-only.
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
        return str(errs[0].get("message", ""))[:120]
    return str(body.get("message", ""))[:120]


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
        "limit": 3,
    }

    print("=" * 78)
    print(f"Ступени ecommerce-воронки · счётчик {counter} · {DATE1}..{DATE2}")
    print("=" * 78)

    # Формы имён под шаги «кликнули по товару» и «посмотрели товар (карточку)».
    # В data layer ecommerce действия называются detail и click — пробую единственное число,
    # множественное, и суффиксы Quantity/Uniq/Price, которыми Метрика оформляет остальные шаги.
    candidates: list[str] = []
    for stem in (
        "productDetail", "productDetails", "productDetailView", "productDetailViews",
        "productCard", "productCardView", "productCardViews",
        "productClick", "productClicks", "productClicked",
        "productView", "productViewed", "productViews",
        "productList", "productListClick",
    ):
        for suffix in ("", "Quantity", "Uniq", "Price"):
            candidates.append(f"ym:s:{stem}{suffix}")

    found: list[tuple[str, float]] = []
    for metric in candidates:
        status, body = metrika({**base, "metrics": metric, "dimensions": "ym:s:productName"}, token)
        if status == 200:
            total = (body.get("totals") or [None])[0]
            print(f"  OK   {metric:<40} итог={total}")
            found.append((metric, float(total or 0)))
        time.sleep(0.25)
    if not found:
        print("  ни одна форма не принята — ни одного 200")

    print("\n--- Контроль: уже известные ступени за тот же период ---")
    known = (
        "ym:s:users,ym:s:visits,"
        "ym:s:productImpressions,ym:s:productImpressionsUniq,"
        "ym:s:productBasketsQuantity,ym:s:productBasketsUniq,"
        "ym:s:productPurchasedQuantity,ym:s:productPurchasedUniq,ym:s:productPurchasedPrice"
    )
    status, body = metrika({**base, "metrics": known, "dimensions": "ym:s:date", "limit": 1}, token)
    print(f"  HTTP {status} totals={body.get('totals')}")
    print(f"  порядок: {known.split(',')}")

    if not found:
        return

    print("\n--- Скрещивание найденных ступеней с товаром / источником / объявлением ---")
    new_metrics = ",".join(m for m, _ in found[:8])
    for dims, label in (
        ("ym:s:date,ym:s:productName", "date × товар"),
        ("ym:s:date,ym:s:lastsignTrafficSource,ym:s:productName", "date × источник × товар"),
        ("ym:s:date,ym:s:lastsignDirectBanner,ym:s:productName", "date × объявление × товар"),
    ):
        status, body = metrika({**base, "metrics": new_metrics, "dimensions": dims, "limit": 2}, token)
        print(f"  {label:<34} HTTP {status} строк={body.get('total_rows')} {short_err(body) if status != 200 else ''}")
        for d in (body.get("data") or [])[:2]:
            print("     ", [x.get("name") for x in d.get("dimensions", [])], d.get("metrics"))
        time.sleep(0.4)

    print("\n--- Объём витрины за август (все ступени вместе, дневная грань) ---")
    full = known + "," + new_metrics
    for dims, label in (
        ("ym:s:date,ym:s:lastsignTrafficSource,ym:s:lastsignUTMCampaign,ym:s:productName", "источник×кампания×товар"),
        ("ym:s:date,ym:s:lastsignDirectBanner,ym:s:productName", "объявление×товар"),
    ):
        status, body = metrika({**base, "metrics": full, "dimensions": dims, "limit": 1}, token)
        print(f"  {label:<26} HTTP {status} строк={body.get('total_rows')} {short_err(body) if status != 200 else ''}")
        time.sleep(0.4)


if __name__ == "__main__":
    main()
