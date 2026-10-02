"""Подбор заужения сегмента BJORN за 90 дней — под Яндекс Аудитории.

Аудитории строят сегмент по последним 90 дням и своего окна не имеют, поэтому дату в
выражение не кладём. Нужно выбрать условия, которые (а) сужают до качественного ядра,
(б) собираются руками в интерфейсе Метрики — через API видимый Аудиториям сегмент не создать.

Заодно проверяем, какие атрибуты «количество визитов на посетителя» вообще существуют:
кандидаты шлём по одному и смотрим, что не отдаёт 400.
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import requests

STAT = "https://api-metrika.yandex.net/stat/v1/data"
DAYS = 90
GOAL_CART = 341059939
GOAL_CART_RUSH = 340578365
GOAL_CHECKOUT = 462482132
GOAL_PURCHASE = 341172800
CORE = "ym:pv:URL=~'/(catalog|product)/'"
NO_BUY = f"NOT(ym:s:goal{GOAL_PURCHASE}IsReached=='Yes')"


def main() -> None:
    token = os.environ["METRICA_TOKEN"].strip()
    counter = os.environ["METRICA_COUNTER_ID"].strip()
    h = {"Authorization": f"OAuth {token}"}
    d2 = (date.today() - timedelta(days=1)).isoformat()
    d1 = (date.today() - timedelta(days=DAYS)).isoformat()
    print(f"период {d1}..{d2} ({DAYS} дней)\n")

    def users(label: str, filt: str) -> None:
        r = requests.get(STAT, headers=h, params={
            "ids": counter, "metrics": "ym:s:users,ym:s:visits",
            "date1": d1, "date2": d2, "accuracy": "full", "filters": filt}, timeout=180)
        if r.status_code != 200:
            print(f"  {label:<58} ОШИБКА {r.status_code} {r.text[:120]}")
            return
        t = r.json().get("totals", [0, 0])
        print(f"  {label:<58} людей {int(t[0]):>7}  визитов {int(t[1]):>7}")

    print("### БАЗА")
    users("весь трафик", "ym:s:visits>0")
    users("каталог или карточка", CORE)
    users("каталог/карточка + не отказ", f"{CORE} AND ym:s:bounce=='No'")
    users("каталог/карточка + не отказ, без купивших",
          f"{CORE} AND ym:s:bounce=='No' AND {NO_BUY}")

    print("\n### СУЩЕСТВУЕТ ЛИ «КОЛИЧЕСТВО ВИЗИТОВ НА ПОСЕТИТЕЛЯ»")
    for attr in ("ym:u:userVisits", "ym:s:userVisits", "ym:u:visits",
                 "ym:s:visitsCount", "ym:u:userVisitsCount", "ym:s:isNewUser"):
        users(f"кандидат {attr}>=2", f"{attr}>=2")

    print("\n### ГЛУБИНА И ВРЕМЯ (поверх ядра, без купивших)")
    base = f"{CORE} AND ym:s:bounce=='No' AND {NO_BUY}"
    for n in (2, 3, 4, 5):
        users(f"+ страниц за визит >= {n}", f"{base} AND ym:s:pageViews>={n}")
    for sec in (30, 60, 120, 180):
        users(f"+ время на сайте >= {sec} с", f"{base} AND ym:s:visitDuration>={sec}")

    print("\n### КАРТОЧКА ТОВАРА КАК ПРИЗНАК ИНТЕРЕСА")
    users("карточка товара + не отказ, без купивших",
          f"ym:pv:URL=@'/product' AND ym:s:bounce=='No' AND {NO_BUY}")
    users("карточка + 2+ страницы, без купивших",
          f"ym:pv:URL=@'/product' AND ym:s:pageViews>=2 AND {NO_BUY}")
    users("карточка + 60 с, без купивших",
          f"ym:pv:URL=@'/product' AND ym:s:visitDuration>=60 AND {NO_BUY}")

    print("\n### ЦЕЛИ КАК ПРИЗНАК (90 дней)")
    users("корзина ecommerce, без купивших", f"ym:s:goal{GOAL_CART}IsReached=='Yes' AND {NO_BUY}")
    users("корзина RUSH, без купивших", f"ym:s:goal{GOAL_CART_RUSH}IsReached=='Yes' AND {NO_BUY}")
    users("начало оформления, без купивших",
          f"ym:s:goal{GOAL_CHECKOUT}IsReached=='Yes' AND {NO_BUY}")
    users("купившие (для вычета)", f"ym:s:goal{GOAL_PURCHASE}IsReached=='Yes'")

    print("\n### ПОВТОРНЫЕ ПОСЕЩЕНИЯ")
    users("не первый визит (ym:s:isNewUser=='No')", "ym:s:isNewUser=='No'")
    users("ядро + не первый визит", f"{base} AND ym:s:isNewUser=='No'")


if __name__ == "__main__":
    main()
