"""Из чего собрать широкий сегмент «качественный трафик» на BJORN за 30 дней.

Считаем охват кандидатов в людях через Reports API Метрики: сегмент должен быть достаточно
широким для Яндекс Аудиторий и при этом отсекать случайный трафик. Только чтение.
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import requests

MGMT = "https://api-metrika.yandex.net/management/v1"
STAT = "https://api-metrika.yandex.net/stat/v1/data"
DAYS = 30


def main() -> None:
    token = os.environ["METRICA_TOKEN"].strip()
    counter = os.environ["METRICA_COUNTER_ID"].strip()
    h = {"Authorization": f"OAuth {token}"}
    d2 = (date.today() - timedelta(days=1)).isoformat()
    d1 = (date.today() - timedelta(days=DAYS)).isoformat()
    print(f"счётчик {counter} | период {d1}..{d2}\n")

    g = requests.get(f"{MGMT}/counter/{counter}/goals", headers=h, timeout=60)
    goals = g.json().get("goals", []) if g.status_code == 200 else []
    print(f"### ЦЕЛИ ({len(goals)})")
    for go in goals:
        print(f"  {go.get('id')} | {go.get('name')} | {go.get('type')}")

    def users(label: str, filt: str | None = None) -> None:
        params = {
            "ids": counter, "metrics": "ym:s:users,ym:s:visits",
            "date1": d1, "date2": d2, "accuracy": "full",
        }
        if filt:
            params["filters"] = filt
        r = requests.get(STAT, headers=h, params=params, timeout=120)
        if r.status_code != 200:
            print(f"  {label:<52} ОШИБКА {r.status_code} {r.text[:160]}")
            return
        totals = r.json().get("totals", [0, 0])
        print(f"  {label:<52} людей {int(totals[0]):>8}  визитов {int(totals[1]):>8}")

    print("\n### ТОП URL (куда реально ходят)")
    r = requests.get(STAT, headers=h, params={
        "ids": counter, "metrics": "ym:pv:pageviews", "dimensions": "ym:pv:URLPathFull",
        "date1": d1, "date2": d2, "limit": 20, "sort": "-ym:pv:pageviews"}, timeout=120)
    if r.status_code == 200:
        for row in r.json().get("data", []):
            print(f"  {int(row['metrics'][0]):>8}  {row['dimensions'][0]['name']}")
    else:
        print(f"  ОШИБКА {r.status_code} {r.text[:200]}")

    print("\n### КАНДИДАТЫ В СЕГМЕНТ")
    users("весь трафик")
    users("не отказ", "ym:s:bounce=='No'")
    users("не отказ + 2+ страницы", "ym:s:bounce=='No' AND ym:s:pageViews>=2")
    users("не отказ + 3+ страницы", "ym:s:bounce=='No' AND ym:s:pageViews>=3")
    users("время на сайте 60с+", "ym:s:visitDuration>=60")
    users("время на сайте 120с+", "ym:s:visitDuration>=120")
    users("не отказ + 60с+", "ym:s:bounce=='No' AND ym:s:visitDuration>=60")
    users("смотрел карточку товара", "ym:pv:URL=@'/product'")
    users("смотрел каталог", "ym:pv:URL=@'/collection'")
    users("положил в корзину", "ym:s:productBasketsQuantity>0")
    users("дошёл до оформления", "ym:pv:URL=@'/checkout'")
    users("достиг любой цели", "ym:s:goalDimension!n")
    users("был 2+ визита", "ym:s:userVisits>=2")

    print("\n### КОМБИНАЦИЯ ДЛЯ СЕГМЕНТА (интерес минус покупка)")
    buy = next((go["id"] for go in goals
                if "покуп" in str(go.get("name")).lower()
                or str(go.get("type")) == "ecommerce"), None)
    print(f"  цель покупки: {buy}")
    if buy:
        base = ("(ym:s:bounce=='No' AND ym:s:pageViews>=2) "
                "OR ym:s:visitDuration>=60 OR ym:s:productBasketsQuantity>0")
        users("интерес (не отказ 2+стр ИЛИ 60с+ ИЛИ корзина)", base)
        users("то же, кроме купивших",
              f"({base}) AND NOT(ym:s:goal{buy}IsReached=='Yes')")


if __name__ == "__main__":
    sys.exit(main())
