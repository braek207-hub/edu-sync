"""Аудитория карточек BJORN: по точному URL объявления и по модели целиком (все цвета).

Первая версия считала неверно: строки вида /product/x/?utm_... после отсечения query
затирали друг друга в словаре, поэтому 30-дневные значения выходили по единице.
Здесь пользователи суммируются, а к каждой группе 714000003 привязан slug-токен модели —
человек, смотревший другой цвет той же куртки, для ретаргетинга такой же годный.

Порог показа условия подбора аудитории в Директе — около 100 человек.

Read-only.
"""

from __future__ import annotations

import os
from collections import defaultdict

import requests

STAT = "https://api-metrika.yandex.net/stat/v1/data"

# группа кампании 714000003 -> (точный URL объявления, slug модели)
GROUPS = [
    ("МОРОЗ · М · Нарвик нат.пух", "/product/muzhskaya-kurtka-alyaska-narvik-na-naturalnom-pukhe-bezhevaya/", "narvik"),
    ("МОРОЗ · М · Ставангер нат.пух", "/product/muzhskaya-udlinennaya-parka-alyaska-stavanger-naturalnyy-pukh-chyernaya/", "stavanger"),
    ("МОРОЗ · М · Тромсо", "/product/zimnyaya-kurtka-alyaska-troms-zelenaya/", "troms"),
    ("МОРОЗ · М · Нарвик", "/product/muzhskaya-kurtka-alyaska-narvik-krasnaya/", "narvik"),
    ("МОРОЗ · М · Рускеала", "/product/muzhskaya-uteplennaya-kurtka-ruskeala-seraya/", "ruskeala"),
    ("МОРОЗ · М · Котлин", "/product/muzhskaya-kurtka-kotlin-chernaya/", "kotlin"),
    ("МОРОЗ · М · Туутари", "/product/muzhskaya-kurtka-tuutari-olivkovaya/", "tuutari"),
    ("МОРОЗ · Ж · Драммен", "/product/zhenskaya-zimnyaya-kurtka-parka-drammen-svetlo-bezhevaya/", "drammen"),
    ("МОРОЗ · Ж · Скиен нат.пух", "/product/parka-zhenskaya-skien-na-naturalnom-pukhe-krasnaya/", "skien"),
    ("МОРОЗ · Ж · Скиен", "/product/parka-zhenskaya-skien-krasnaya/", "skien"),
    ("МОРОЗ · Ж · Вуокса", "/product/zhenskaya-uteplennaya-kurtka-vuoksa-terrakotovaya/", "vuoksa"),
    ("МОРОЗ · Ж · Монферрана", "/product/zhenskaya-uteplennaya-kurtka-monferrana-seraya/", "monferrana"),
    ("МОРОЗ · Ж · Игора", "/product/zhenskaya-uteplennaya-kurtka-igora-chernaya/", "igora"),
    ("МОРОЗ · Ж · Карелия", "/product/zhenskaya-uteplennaya-kurtka-kareliya-chernaya/", "kareliya"),
    ("ЛАЙТ · М · Ставангер", "/product/muzhskaya-udlinennaya-parka-alyaska-stavanger-seraya/", "stavanger"),
    ("ЛАЙТ · М · Хамар", "/product/muzhskoy-demisezonnyy-bomber-hamar-tyemno-zelenyy/", "hamar"),
    ("МЕЖСЕЗ · М · Кронштадт", "/product/muzhskaya-demisezonnaya-kurtka-kronshtadt-seraya/", "kronshtadt"),
    ("МЕЖСЕЗ · М · Тонсберг", "/product/muzhskaya-demisezonnaya-parka-tonsberg-khaki/", "tonsberg"),
    ("МЕЖСЕЗ · Ж · Альта", "/product/zhenskaya-demisezonnaya-parka-alta-sinyaya/", "-alta-"),
    ("МЕЖСЕЗ · Ж · Монрепо", "/product/zhenskaya-demisezonnaya-kurtka-monrepo-belaya/", "monrepo"),
    ("МЕЖСЕЗ · Ж · Оланга", "/product/zhenskaya-kurtka-olanga-bordovaya/", "olanga"),
    ("ПРОХЛАДА · М · Аскер", "/product/muzhskoy-demisezonnyy-legkiy-pukhovik-asker-chernyy/", "asker"),
    ("ПРОХЛАДА · М · Ларвик", "/product/muzhskoy-demisezonnyy-bomber-kurtka-pilot-larvik-chernyy/", "larvik"),
    ("ПРОХЛАДА · М · Кареджи", "/product/zhilet-muzhskoy-uteplennyy-karedzhi-khaki/", "karedzhi"),
    ("ПРОХЛАДА · Ж · Лиллестром", "/product/zhenskiy-demisezonnyy-legkiy-pukhovik-lillestrom-siniy/", "lillestrom"),
    ("ДОЖДЬ · М · Аккала", "/product/vetrovka-muzhskaya-akkala-temno-seraya/", "akkala"),
    ("ДОЖДЬ · М · Комарово", "/product/vetrovka-muzhskaya-komarovo-sinyaya/", "komarovo"),
    ("ДОЖДЬ · Ж · Репино", "/product/zhenskaya-vetrovka-repino-temno-seraya/", "repino"),
    ("ДОЖДЬ · Ж · Миэлиси", "/product/zhenskaya-vetrovka-mielisi-golubaya/", "mielisi"),
    ("ДОЖДЬ · Ж · Охта", "/product/zhenskaya-vetrovka-okhta-khaki/", "okhta"),
    ("КОЖА · М · Выборг", "/product/muzhskoy-demisezonnyy-bomber-vyborg-chernyy/", "vyborg"),
    ("КОЖА · М · Котлас", "/product/muzhskaya-kurtka-bomber-iz-naturalnoy-kozhi-kotlas-chernaya/", "kotlas"),
    ("КОЖА · Ж · Ладога", "/product/zhenskaya-demisezonnaya-kosukha-ladoga-krasnaya/", "ladoga"),
]


def fetch(h: dict, counter: str, date1: str) -> dict[str, float]:
    users: dict[str, float] = defaultdict(float)
    offset = 1
    while True:
        r = requests.get(STAT, headers=h, params={
            "ids": counter,
            "metrics": "ym:pv:users",
            "dimensions": "ym:pv:URLPathFull",
            "filters": "ym:pv:URLPathFull=@'/product/'",
            "date1": date1, "date2": "yesterday",
            "limit": 5000, "offset": offset, "sort": "-ym:pv:users",
            "accuracy": "full",
        }, timeout=180)
        if r.status_code != 200:
            print(f"  HTTP {r.status_code} {r.text[:300]}")
            break
        body = r.json()
        rows = body.get("data", [])
        for row in rows:
            path = row["dimensions"][0]["name"].split("?")[0]
            if not path.endswith("/"):
                path += "/"
            users[path] += row["metrics"][0]
        if len(rows) < 5000:
            break
        offset += 5000
    return users


def main() -> None:
    h = {"Authorization": f"OAuth {os.environ['METRICA_TOKEN'].strip()}"}
    counter = os.environ["METRICA_COUNTER_ID"].strip()

    w = {d: fetch(h, counter, d) for d in ("7daysAgo", "30daysAgo")}
    for d, data in w.items():
        print(f"{d}: карточек {len(data)}, людей суммарно {sum(data.values()):.0f}")

    def by_token(data: dict[str, float], token: str) -> float:
        return sum(v for k, v in data.items() if token in k)

    print(f"\n{'группа':30} {'URL 7д':>8} {'URL 30д':>8} {'модель 7д':>10} {'модель 30д':>11}")
    bad_url7 = bad_model7 = bad_model30 = 0
    for name, path, token in GROUPS:
        u7 = w["7daysAgo"].get(path, 0)
        u30 = w["30daysAgo"].get(path, 0)
        m7 = by_token(w["7daysAgo"], token)
        m30 = by_token(w["30daysAgo"], token)
        bad_url7 += u7 < 100
        bad_model7 += m7 < 100
        bad_model30 += m30 < 100
        print(f"{name:30} {u7:8.0f} {u30:8.0f} {m7:10.0f} {m30:11.0f}")

    n = len(GROUPS)
    print(f"\n  ниже порога 100 человек:")
    print(f"    точный URL, 7 дней:  {bad_url7} из {n}")
    print(f"    модель, 7 дней:      {bad_model7} из {n}")
    print(f"    модель, 30 дней:     {bad_model30} из {n}")


if __name__ == "__main__":
    main()
