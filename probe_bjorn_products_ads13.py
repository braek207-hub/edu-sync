# Проба BJORN, раунд 13. Финальная перед реализацией: проверяю не «есть ли такие метрики
# вообще», а РОВНО ТОТ запрос, который будет делать sync_products — полный набор метрик
# (включая уники всех четырёх ступеней) в полной шестимерной группировке.
# Если он отдаёт 200 — в плане нет ни одного угаданного имени.
from __future__ import annotations

import os

import requests

STAT = "https://api-metrika.yandex.net/stat/v1/data"
DATE1 = os.environ.get("PROBE_DATE1", "2026-08-01")
DATE2 = os.environ.get("PROBE_DATE2", "2026-08-27")
TOKEN = os.environ["METRICA_TOKEN"]
COUNTER = os.environ["METRICA_COUNTER_ID"]
HEADERS = {"Authorization": f"OAuth {TOKEN}"}

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

DIMS_TPL = [
    "ym:s:date",
    "ym:s:<a>TrafficSource",
    "ym:s:<a>UTMCampaign",
    "ym:s:<a>DirectBanner",
    "ym:s:productID",
    "ym:s:productName",
]


def call(params: dict) -> requests.Response:
    return requests.get(STAT, params=params, headers=HEADERS, timeout=180)


def main() -> None:
    print("=" * 78)
    print("1. Каждая метрика по отдельности (ловим точное имя)")
    print("=" * 78)
    ok_metrics = []
    for m in METRICS:
        resp = call({
            "ids": COUNTER, "date1": DATE1, "date2": DATE2, "accuracy": "full",
            "proposed_accuracy": "false", "lang": "ru", "limit": 1,
            "attribution": "lastsign", "metrics": m, "dimensions": "ym:s:productName",
        })
        if resp.status_code == 200:
            ok_metrics.append(m)
            print(f"  OK  {m:<36} totals={resp.json().get('totals')}")
        else:
            print(f"  НЕТ {m:<36} {resp.text[:160]}")

    print("\n" + "=" * 78)
    print("2. Полный запрос синка: все принятые метрики × шесть измерений")
    print("=" * 78)
    for attr in ("automatic", "first", "lastsign"):
        dims = ",".join(d.replace("<a>", attr) for d in DIMS_TPL)
        resp = call({
            "ids": COUNTER, "date1": DATE1, "date2": DATE2, "accuracy": "full",
            "proposed_accuracy": "false", "lang": "ru", "limit": 100000, "offset": 1,
            "attribution": attr, "metrics": ",".join(ok_metrics), "dimensions": dims,
        })
        if resp.status_code != 200:
            print(f"  {attr}: HTTP {resp.status_code} {resp.text[:250]}")
            continue
        body = resp.json()
        rows = body.get("data", [])
        print(f"  {attr}: строк={len(rows)} всего={body.get('total_rows')} totals={body.get('totals')}")
        if attr == "lastsign":
            print(f"    измерения: {dims}")
            for d in rows[:3]:
                print("     ", [str(x.get("name"))[:30] for x in d["dimensions"]], d["metrics"])
            banners = {str(d["dimensions"][3].get("name")) for d in rows}
            with_m = sorted(b for b in banners if b and b.startswith("M-"))
            print(f"    различных значений объявления: {len(banners)}, из них с префиксом M-: {len(with_m)}")
            print(f"    примеры: {with_m[:4]}")
            print(f"    пустых/None: {sum(1 for b in banners if not b or b in ('null', 'None'))}")


if __name__ == "__main__":
    main()
