"""Сколько людей смотрит каждую карточку куртки за 7 и за 30 дней.

Это решает, реализуема ли идея «ручной офферный ретаргетинг по товару»: условие подбора
аудитории в Директе начинает показываться от ~100 пользователей в сегменте. Если карточку
за неделю смотрят 20 человек, группа под неё не выйдет из нулевого охвата.

Плюс проверяем, есть ли у токена права на запись в Метрику (нужны для 33 целей по URL).

Read-only.
"""

from __future__ import annotations

import json
import os

import requests

METRIKA = "https://api-metrika.yandex.net/management/v1"
STAT = "https://api-metrika.yandex.net/stat/v1/data"


def rights(h: dict, counter: str) -> None:
    print("### ПРАВА НА СЧЁТЧИК")
    r = requests.get(f"{METRIKA}/counter/{counter}", headers=h, timeout=60)
    if r.status_code != 200:
        print(f"  HTTP {r.status_code} {r.text[:250]}")
        return
    c = r.json().get("counter", {})
    keys = {k: c.get(k) for k in ("id", "name", "site", "permission", "owner_login", "status")}
    print(f"  {json.dumps(keys, ensure_ascii=False)}")
    gr = c.get("grants")
    if gr is not None:
        print(f"  grants: {json.dumps(gr, ensure_ascii=False)[:500]}")


def audience(h: dict, counter: str, days: str, label: str) -> dict[str, dict]:
    print(f"\n### АУДИТОРИЯ КАРТОЧЕК ТОВАРОВ — {label}")
    out: dict[str, dict] = {}
    offset = 1
    while True:
        r = requests.get(STAT, headers=h, params={
            "ids": counter,
            "metrics": "ym:pv:users,ym:pv:pageviews",
            "dimensions": "ym:pv:URLPathFull",
            "filters": "ym:pv:URLPathFull=@'/product/'",
            "date1": days,
            "date2": "yesterday",
            "limit": 500,
            "offset": offset,
            "sort": "-ym:pv:users",
        }, timeout=120)
        if r.status_code != 200:
            print(f"  HTTP {r.status_code} {r.text[:300]}")
            return out
        body = r.json()
        rows = body.get("data", [])
        for row in rows:
            path = row["dimensions"][0]["name"]
            out[path.split("?")[0].rstrip("/") + "/"] = {
                "users": row["metrics"][0], "pv": row["metrics"][1]}
        if len(rows) < 500:
            break
        offset += 500
    print(f"  карточек с трафиком: {len(out)} | всего строк учтено")
    return out


def main() -> None:
    token = os.environ.get("METRICA_TOKEN", "").strip()
    counter = os.environ.get("METRICA_COUNTER_ID", "").strip()
    h = {"Authorization": f"OAuth {token}"}
    rights(h, counter)

    w7 = audience(h, counter, "7daysAgo", "последние 7 дней")
    w30 = audience(h, counter, "30daysAgo", "последние 30 дней")

    products = [
        ("МОРОЗ · М · Нарвик нат.пух", "/product/muzhskaya-kurtka-alyaska-narvik-na-naturalnom-pukhe-bezhevaya/"),
        ("МОРОЗ · М · Ставангер нат.пух", "/product/muzhskaya-udlinennaya-parka-alyaska-stavanger-naturalnyy-pukh-chyernaya/"),
        ("МОРОЗ · М · Тромсо", "/product/zimnyaya-kurtka-alyaska-troms-zelenaya/"),
        ("МОРОЗ · М · Нарвик", "/product/muzhskaya-kurtka-alyaska-narvik-krasnaya/"),
        ("МОРОЗ · М · Рускеала", "/product/muzhskaya-uteplennaya-kurtka-ruskeala-seraya/"),
        ("МОРОЗ · М · Котлин", "/product/muzhskaya-kurtka-kotlin-chernaya/"),
        ("МОРОЗ · М · Туутари", "/product/muzhskaya-kurtka-tuutari-olivkovaya/"),
        ("МОРОЗ · Ж · Драммен", "/product/zhenskaya-zimnyaya-kurtka-parka-drammen-svetlo-bezhevaya/"),
        ("МОРОЗ · Ж · Скиен нат.пух", "/product/parka-zhenskaya-skien-na-naturalnom-pukhe-krasnaya/"),
        ("МОРОЗ · Ж · Скиен", "/product/parka-zhenskaya-skien-krasnaya/"),
        ("МОРОЗ · Ж · Вуокса", "/product/zhenskaya-uteplennaya-kurtka-vuoksa-terrakotovaya/"),
        ("МОРОЗ · Ж · Монферрана", "/product/zhenskaya-uteplennaya-kurtka-monferrana-seraya/"),
        ("МОРОЗ · Ж · Игора", "/product/zhenskaya-uteplennaya-kurtka-igora-chernaya/"),
        ("МОРОЗ · Ж · Карелия", "/product/zhenskaya-uteplennaya-kurtka-kareliya-chernaya/"),
        ("ЛАЙТ · М · Ставангер", "/product/muzhskaya-udlinennaya-parka-alyaska-stavanger-seraya/"),
        ("ЛАЙТ · М · Хамар", "/product/muzhskoy-demisezonnyy-bomber-hamar-tyemno-zelenyy/"),
        ("МЕЖСЕЗОНЬЕ · М · Кронштадт", "/product/muzhskaya-demisezonnaya-kurtka-kronshtadt-seraya/"),
        ("МЕЖСЕЗОНЬЕ · М · Тонсберг", "/product/muzhskaya-demisezonnaya-parka-tonsberg-khaki/"),
        ("МЕЖСЕЗОНЬЕ · Ж · Альта", "/product/zhenskaya-demisezonnaya-parka-alta-sinyaya/"),
        ("МЕЖСЕЗОНЬЕ · Ж · Монрепо", "/product/zhenskaya-demisezonnaya-kurtka-monrepo-belaya/"),
        ("МЕЖСЕЗОНЬЕ · Ж · Оланга", "/product/zhenskaya-kurtka-olanga-bordovaya/"),
        ("ПРОХЛАДА · М · Аскер", "/product/muzhskoy-demisezonnyy-legkiy-pukhovik-asker-chernyy/"),
        ("ПРОХЛАДА · М · Ларвик", "/product/muzhskoy-demisezonnyy-bomber-kurtka-pilot-larvik-chernyy/"),
        ("ПРОХЛАДА · М · Кареджи", "/product/zhilet-muzhskoy-uteplennyy-karedzhi-khaki/"),
        ("ПРОХЛАДА · Ж · Лиллестром", "/product/zhenskiy-demisezonnyy-legkiy-pukhovik-lillestrom-siniy/"),
        ("ДОЖДЬ · М · Аккала", "/product/vetrovka-muzhskaya-akkala-temno-seraya/"),
        ("ДОЖДЬ · М · Комарово", "/product/vetrovka-muzhskaya-komarovo-sinyaya/"),
        ("ДОЖДЬ · Ж · Репино", "/product/zhenskaya-vetrovka-repino-temno-seraya/"),
        ("ДОЖДЬ · Ж · Миэлиси", "/product/zhenskaya-vetrovka-mielisi-golubaya/"),
        ("ДОЖДЬ · Ж · Охта", "/product/zhenskaya-vetrovka-okhta-khaki/"),
        ("КОЖА · М · Выборг", "/product/muzhskoy-demisezonnyy-bomber-vyborg-chernyy/"),
        ("КОЖА · М · Котлас", "/product/muzhskaya-kurtka-bomber-iz-naturalnoy-kozhi-kotlas-chernaya/"),
        ("КОЖА · Ж · Ладога", "/product/zhenskaya-demisezonnaya-kosukha-ladoga-krasnaya/"),
    ]

    print("\n### ПО ГРУППАМ КАМПАНИИ 714000003 (люди на карточке)")
    print(f"{'группа':32} {'7 дней':>10} {'30 дней':>10}")
    lo7 = 0
    for name, path in products:
        u7 = w7.get(path, {}).get("users", 0)
        u30 = w30.get(path, {}).get("users", 0)
        if u7 < 100:
            lo7 += 1
        print(f"{name:32} {u7:10.0f} {u30:10.0f}")
    print(f"\n  карточек, где за 7 дней меньше 100 человек: {lo7} из {len(products)}")

    tot7 = sum(v["users"] for v in w7.values())
    tot30 = sum(v["users"] for v in w30.values())
    print(f"  всего людей на карточках: 7д={tot7:.0f} | 30д={tot30:.0f}")

    print("\n### ТОП-15 карточек по людям за 7 дней (что вообще смотрят)")
    for path, v in sorted(w7.items(), key=lambda kv: -kv[1]["users"])[:15]:
        print(f"  {v['users']:7.0f} чел | {path}")


if __name__ == "__main__":
    main()
