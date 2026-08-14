# -*- coding: utf-8 -*-
"""
probe_metrika_fields.py — какие измерения и метрики Метрики годятся автопилоту.

Первый заход вернул ноль строк без исключений: запросы прошли, но выборка пуста —
значит имена измерений угаданы неверно (писал lastsign, рабочий код использует last).
Вместо гадания — перебираем варианты и печатаем, сколько строк вернулось.

Запуск: python probe_metrika_fields.py
ENV: YM_TOKEN
"""

import os
from datetime import date, timedelta
from typing import Any, Dict, List

import requests

METRICA_API_URL = "https://api-metrika.yandex.net/stat/v1/data"
COUNTER = 98627983  # vuz — основной счётчик EDU

# (подпись, dimensions, metrics, filters)
CASES: List[tuple] = [
    ("часы: ym:s:hour", "ym:s:hour", "ym:s:visits", None),
    ("часы: ym:s:startOfHour", "ym:s:startOfHour", "ym:s:visits", None),
    ("кампании last + Name", "ym:s:lastDirectClickOrderName",
     "ym:s:visits,ym:s:bounceRate,ym:s:pageDepth,ym:s:avgVisitDurationSeconds", None),
    ("кампании last (ID)", "ym:s:lastDirectClickOrder",
     "ym:s:visits,ym:s:bounceRate,ym:s:pageDepth,ym:s:avgVisitDurationSeconds", None),
    ("кампании lastsign (ID)", "ym:s:lastsignDirectClickOrder", "ym:s:visits", None),
    ("кампании last + фильтр ad", "ym:s:lastDirectClickOrderName", "ym:s:visits",
     "ym:s:lastTrafficSource=='ad'"),
    ("цели: goalDimensionVisits", "ym:s:hour", "ym:s:goalDimensionVisits", None),
    ("цели: sumGoalReachesAny", "ym:s:hour", "ym:s:sumGoalReachesAny", None),
]


def probe(label: str, dims: str, metrics: str, filters: str) -> str:
    params: Dict[str, Any] = {
        "ids": COUNTER,
        "metrics": metrics,
        "dimensions": dims,
        "date1": (date.today() - timedelta(days=14)).isoformat(),
        "date2": date.today().isoformat(),
        "limit": 10,
        "accuracy": "full",
    }
    if filters:
        params["filters"] = filters
    resp = requests.get(
        METRICA_API_URL, params=params,
        headers={"Authorization": f"OAuth {os.environ['YM_TOKEN']}"}, timeout=90,
    )
    resp.encoding = "utf-8"
    if resp.status_code != 200:
        return f"HTTP {resp.status_code}: {resp.text[:160]}"
    rows = resp.json().get("data") or []
    if not rows:
        return "0 строк (валидно, но пусто)"
    sample = rows[0]
    dim = (sample.get("dimensions") or [{}])[0].get("name")
    return f"{len(rows)} строк, пример: {dim!r} → {sample.get('metrics')}"


def main() -> int:
    for label, dims, metrics, filters in CASES:
        try:
            print(f"{label:32} {probe(label, dims, metrics, filters)}")
        except Exception as exc:
            print(f"{label:32} ОШИБКА {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
