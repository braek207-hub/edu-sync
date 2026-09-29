"""Аудитория «смотрел модель за последние 10 дней» по всем 33 группам 714000003.

Выражение проверено прогоном: ym:pv:URL=@'<slug>' AND ym:s:date>='<дата>' Метрика принимает,
то есть 10-дневное окно закладывается в сам сегмент, а не в срок правила Директа
(для сегментов срок игнорируется — там всегда 540 дней).

Порог, ниже которого сегмент не показывается, — 100 человек.

Read-only.
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import requests

STAT = "https://api-metrika.yandex.net/stat/v1/data"
WINDOW_DAYS = 10

MODELS = [
    ("МОРОЗ · М · Нарвик нат.пух", "narvik"),
    ("МОРОЗ · М · Ставангер нат.пух", "stavanger"),
    ("МОРОЗ · М · Тромсо", "troms"),
    ("МОРОЗ · М · Нарвик", "narvik"),
    ("МОРОЗ · М · Рускеала", "ruskeala"),
    ("МОРОЗ · М · Котлин", "kotlin"),
    ("МОРОЗ · М · Туутари", "tuutari"),
    ("МОРОЗ · Ж · Драммен", "drammen"),
    ("МОРОЗ · Ж · Скиен нат.пух", "skien"),
    ("МОРОЗ · Ж · Скиен", "skien"),
    ("МОРОЗ · Ж · Вуокса", "vuoksa"),
    ("МОРОЗ · Ж · Монферрана", "monferrana"),
    ("МОРОЗ · Ж · Игора", "igora"),
    ("МОРОЗ · Ж · Карелия", "kareliya"),
    ("ЛАЙТ · М · Ставангер", "stavanger"),
    ("ЛАЙТ · М · Хамар", "hamar"),
    ("МЕЖСЕЗ · М · Кронштадт", "kronshtadt"),
    ("МЕЖСЕЗ · М · Тонсберг", "tonsberg"),
    ("МЕЖСЕЗ · Ж · Альта", "-alta-"),
    ("МЕЖСЕЗ · Ж · Монрепо", "monrepo"),
    ("МЕЖСЕЗ · Ж · Оланга", "olanga"),
    ("ПРОХЛАДА · М · Аскер", "asker"),
    ("ПРОХЛАДА · М · Ларвик", "larvik"),
    ("ПРОХЛАДА · М · Кареджи", "karedzhi"),
    ("ПРОХЛАДА · Ж · Лиллестром", "lillestrom"),
    ("ДОЖДЬ · М · Аккала", "akkala"),
    ("ДОЖДЬ · М · Комарово", "komarovo"),
    ("ДОЖДЬ · Ж · Репино", "repino"),
    ("ДОЖДЬ · Ж · Миэлиси", "mielisi"),
    ("ДОЖДЬ · Ж · Охта", "okhta"),
    ("КОЖА · М · Выборг", "vyborg"),
    ("КОЖА · М · Котлас", "kotlas"),
    ("КОЖА · Ж · Ладога", "ladoga"),
]


def users(h: dict, counter: str, expr: str) -> float:
    r = requests.get(STAT, headers=h, params={
        "ids": counter, "metrics": "ym:s:users",
        "date1": "30daysAgo", "date2": "yesterday",
        "filters": expr, "limit": 1, "accuracy": "full",
    }, timeout=180)
    if r.status_code != 200:
        print(f"    ошибка {r.status_code}: {r.text[:200]}")
        return -1
    return r.json().get("totals", [0])[0]


def main() -> None:
    h = {"Authorization": f"OAuth {os.environ['METRICA_TOKEN'].strip()}"}
    counter = os.environ["METRICA_COUNTER_ID"].strip()
    since = (date.today() - timedelta(days=WINDOW_DAYS)).isoformat()
    print(f"окно: с {since} по вчера ({WINDOW_DAYS} дней)\n")

    seen: dict[str, float] = {}
    print(f"{'группа':30} {'slug':12} {'человек':>9}  порог 100")
    low = []
    for name, slug in MODELS:
        if slug not in seen:
            seen[slug] = users(h, counter, f"ym:pv:URL=@'{slug}' AND ym:s:date>='{since}'")
        u = seen[slug]
        mark = "ok" if u >= 100 else "МАЛО"
        if u < 100:
            low.append((name, slug, u))
        print(f"{name:30} {slug:12} {u:9.0f}  {mark}")

    print(f"\nуникальных сегментов: {len(seen)} (групп {len(MODELS)})")
    print(f"ниже порога 100: {len(low)}")
    for name, slug, u in low:
        print(f"  {name} ({slug}) — {u:.0f}")

    nb = users(h, counter, f"ym:s:bounce=='No' AND ym:s:date>='{since}'")
    print(f"\nдля справки, не-отказные визиты за окно: {nb:.0f} человек")
    both = users(h, counter,
                 f"ym:pv:URL=@'narvik' AND ym:s:bounce=='No' AND ym:s:date>='{since}'")
    print(f"narvik + не отказ за окно: {both:.0f} человек (против {seen['narvik']:.0f} без фильтра)")


if __name__ == "__main__":
    main()
