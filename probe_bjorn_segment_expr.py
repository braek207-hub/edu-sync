"""Какое выражение сегмента Метрики ловит «смотрел страницу модели» и сколько это людей.

Срок в правилах Директа применяется только к целям: сегмент Метрики отдаёт пользователей
за 540 дней и срок для него игнорируется. Поэтому окно «10 дней» в условии даст не сегмент,
а вторая строка правил — цель «Посетил сайт» (462902408), она в аккаунте давно.

Здесь: 1) валидируем кандидатов выражения через отчёт (принимает ли Метрика синтаксис),
2) заодно проверяем, есть ли атрибут относительной давности визита — если есть, окно можно
   заложить в сам сегмент, 3) пробуем создать первый сегмент по модели narvik.

Пункты 1-2 read-only. Пункт 3 создаёт один сегмент (APPLY=1).
"""

from __future__ import annotations

import json
import os

import requests

METRIKA = "https://api-metrika.yandex.net/management/v1"
STAT = "https://api-metrika.yandex.net/stat/v1/data"

CANDIDATES = [
    ("плоский фильтр по URL просмотра", "ym:pv:URL=@'narvik'"),
    ("EXISTS без пространства", "EXISTS(ym:pv:URL=@'narvik')"),
    ("EXISTS по пользователю", "EXISTS ym:u:userID WITH (ym:pv:URL=@'narvik')"),
    ("EXISTS по визиту", "EXISTS ym:s:visitID WITH (ym:pv:URL=@'narvik')"),
    ("URL визита", "ym:s:URL=@'narvik'"),
    ("страница входа", "ym:s:startURL=@'narvik'"),
    ("давность последнего визита", "ym:u:daysSinceLastVisit<=10"),
    ("давность первого визита", "ym:s:daysSinceFirstVisit<=10"),
    ("URL + давность визита", "ym:pv:URL=@'narvik' AND ym:u:daysSinceLastVisit<=10"),
]


def probe(h: dict, counter: str) -> None:
    print("### ВАЛИДАЦИЯ ВЫРАЖЕНИЙ (отчёт за 365 дней, метрика — люди)")
    for label, expr in CANDIDATES:
        r = requests.get(STAT, headers=h, params={
            "ids": counter, "metrics": "ym:s:users",
            "date1": "365daysAgo", "date2": "yesterday",
            "filters": expr, "limit": 1, "accuracy": "full",
        }, timeout=180)
        if r.status_code != 200:
            msg = r.json().get("message", r.text[:160]) if r.text.startswith("{") else r.text[:160]
            print(f"  ОТКАЗ  {label:32} {expr}\n         → {msg}")
            continue
        totals = r.json().get("totals", [0])
        print(f"  ОК     {label:32} {expr}\n         → людей: {totals[0]:.0f}")


def sizes(h: dict, counter: str, expr_tpl: str, tokens: list[str]) -> None:
    print("\n### РАЗМЕР АУДИТОРИИ ПО МОДЕЛЯМ (за 365 дней, рабочее выражение)")
    for token in tokens:
        expr = expr_tpl.replace("TOKEN", token)
        r = requests.get(STAT, headers=h, params={
            "ids": counter, "metrics": "ym:s:users",
            "date1": "365daysAgo", "date2": "yesterday",
            "filters": expr, "limit": 1, "accuracy": "full",
        }, timeout=180)
        if r.status_code != 200:
            print(f"  {token:12} ошибка {r.status_code}")
            continue
        print(f"  {token:12} {r.json().get('totals', [0])[0]:9.0f} чел")


def create_first(h: dict, counter: str, expr: str) -> None:
    print("\n### ПОПЫТКА СОЗДАТЬ СЕГМЕНТ «RT: смотрел Нарвик»")
    payload = {"segment": {"name": "RT: смотрел Нарвик", "expression": expr}}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    r = requests.post(f"{METRIKA}/counter/{counter}/segments", headers={
        **h, "Content-Type": "application/json; charset=utf-8"}, data=body, timeout=60)
    print(f"  HTTP {r.status_code}")
    print(f"  {r.text[:500]}")
    if r.status_code in (200, 201):
        seg = r.json().get("segment", {})
        print(f"  создан сегмент {seg.get('segment_id')} «{seg.get('name')}»")
        rs = requests.get(f"{METRIKA}/counter/{counter}/segments", headers=h, timeout=60)
        for s in rs.json().get("segments", []):
            print(f"    есть: {s.get('segment_id')} | {s.get('name')} | {s.get('expression')}")


def main() -> None:
    h = {"Authorization": f"OAuth {os.environ['METRICA_TOKEN'].strip()}"}
    counter = os.environ["METRICA_COUNTER_ID"].strip()
    probe(h, counter)

    working = os.environ.get("EXPR", "ym:pv:URL=@'TOKEN'").strip()
    sizes(h, counter, working, ["narvik", "stavanger", "kotlas", "karedzhi", "asker"])

    if os.environ.get("APPLY", "").strip() == "1":
        create_first(h, counter, working.replace("TOKEN", "narvik"))
    else:
        print("\n(APPLY не задан — сегмент не создавался)")


if __name__ == "__main__":
    main()
