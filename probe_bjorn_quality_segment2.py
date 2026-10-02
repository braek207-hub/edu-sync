"""Уточняем охват ядра сегмента BJORN: коммерческий интерес против блогового трафика.

Первый прогон показал: /collection на сайте нет (каталог — /catalog), корзину нельзя
фильтровать метрикой productBasketsQuantity (вернула весь трафик), «любая цель» бесполезна
(цель «Посетил сайт» достигают все), а OR в фильтрах Reports API для этих измерений
запрещён — комбинации считаем по частям. Только чтение.
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import requests

STAT = "https://api-metrika.yandex.net/stat/v1/data"
DAYS = 30
GOAL_CART = 341059939        # Ecommerce: добавление в корзину
GOAL_CART_RUSH = 340578365   # RUSH - Добавить в корзину
GOAL_CHECKOUT = 462482132    # Начало оформления заказа
GOAL_PURCHASE = 341172800    # Ecommerce: покупка


def main() -> None:
    token = os.environ["METRICA_TOKEN"].strip()
    counter = os.environ["METRICA_COUNTER_ID"].strip()
    h = {"Authorization": f"OAuth {token}"}
    d2 = (date.today() - timedelta(days=1)).isoformat()
    d1 = (date.today() - timedelta(days=DAYS)).isoformat()
    print(f"период {d1}..{d2}\n")

    def users(label: str, filt: str) -> None:
        r = requests.get(STAT, headers=h, params={
            "ids": counter, "metrics": "ym:s:users,ym:s:visits",
            "date1": d1, "date2": d2, "accuracy": "full", "filters": filt}, timeout=120)
        if r.status_code != 200:
            print(f"  {label:<56} ОШИБКА {r.status_code} {r.text[:140]}")
            return
        t = r.json().get("totals", [0, 0])
        print(f"  {label:<56} людей {int(t[0]):>7}  визитов {int(t[1]):>7}")

    print("### ГДЕ БЫЛИ")
    users("каталог", "ym:pv:URL=@'/catalog'")
    users("карточка товара", "ym:pv:URL=@'/product'")
    users("каталог или карточка (regexp)", "ym:pv:URL=~'/(catalog|product)/'")
    users("зимние куртки", "ym:pv:URL=@'zimnie-kurtki'")
    users("блог", "ym:pv:URL=@'/blog'")
    users("корзина/оформление", "ym:pv:URL=@'/order'")

    print("\n### КАЧЕСТВО")
    users("каталог/карточка + не отказ",
          "ym:pv:URL=~'/(catalog|product)/' AND ym:s:bounce=='No'")
    users("каталог/карточка + не отказ + 2+ страницы",
          "ym:pv:URL=~'/(catalog|product)/' AND ym:s:bounce=='No' AND ym:s:pageViews>=2")
    users("каталог/карточка + 60с+",
          "ym:pv:URL=~'/(catalog|product)/' AND ym:s:visitDuration>=60")

    print("\n### ЦЕЛИ")
    users("добавление в корзину (ecommerce)", f"ym:s:goal{GOAL_CART}IsReached=='Yes'")
    users("добавление в корзину (RUSH)", f"ym:s:goal{GOAL_CART_RUSH}IsReached=='Yes'")
    users("начало оформления", f"ym:s:goal{GOAL_CHECKOUT}IsReached=='Yes'")
    users("покупка", f"ym:s:goal{GOAL_PURCHASE}IsReached=='Yes'")

    print("\n### ВЫЧЕТ ПОКУПАТЕЛЕЙ")
    users("каталог/карточка + не отказ, кроме купивших",
          f"ym:pv:URL=~'/(catalog|product)/' AND ym:s:bounce=='No' "
          f"AND NOT(ym:s:goal{GOAL_PURCHASE}IsReached=='Yes')")
    users("каталог/карточка + не отказ + 2+ стр, кроме купивших",
          f"ym:pv:URL=~'/(catalog|product)/' AND ym:s:bounce=='No' AND ym:s:pageViews>=2 "
          f"AND NOT(ym:s:goal{GOAL_PURCHASE}IsReached=='Yes')")

    print("\n### РАБОТАЕТ ЛИ OR В ВЫРАЖЕНИИ СЕГМЕНТА (через фильтр того же вида)")
    users("карточка ИЛИ корзина",
          f"ym:pv:URL=@'/product' OR ym:s:goal{GOAL_CART}IsReached=='Yes'")


if __name__ == "__main__":
    main()
