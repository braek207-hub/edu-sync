#!/usr/bin/env python3
"""Что Метрика знает о трафике, который у PROCONTEXT лежит без кампании.

Корзины из аудита 11.09 (web, RU): `ya_direct/ad` 146 тыс. сессий без campaign_id,
`google/cpc` с campaign=`g`, `ya.GO/cpm`. Вопрос: знает ли Метрика кампанию Директа
(по yclid → ym:s:lastsignDirectClickOrder) там, где в UTM её нет, — тогда можно
доразметить своими данными.

Только чтение Stat API, счётчик 23504302, гео Россия.
"""
import os
from datetime import datetime, timedelta, timezone

import requests

API_URL = "https://api-metrika.yandex.net/stat/v1/data"
COUNTER = "23504302"

TO = (os.environ.get("PROBE_TO") or "").strip() or (
    datetime.now(timezone.utc) - timedelta(days=2)
).date().isoformat()
FROM = (os.environ.get("PROBE_FROM") or "").strip() or (
    datetime.fromisoformat(TO) - timedelta(days=13)
).date().isoformat()


def fetch(dimensions, filters, limit=60):
    params = {
        "ids": COUNTER,
        "date1": FROM,
        "date2": TO,
        "metrics": "ym:s:visits,ym:s:ecommercePurchases",
        "dimensions": ",".join(dimensions),
        "filters": f"ym:s:regionCountryName=='Russia' AND ({filters})",
        "sort": "-ym:s:visits",
        "accuracy": "full",
        "limit": limit,
    }
    r = requests.get(API_URL, headers={"Authorization": f"OAuth {os.environ['LIME_METRIKA_TOKEN']}"},
                     params=params, timeout=120)
    r.raise_for_status()
    js = r.json()
    print(f"   всего строк {js.get('total_rows')}, визитов {js.get('totals', [0])[0]}")
    for it in js.get("data", []):
        dims = " | ".join(str(d.get("name")) for d in it["dimensions"])
        v, p = it["metrics"]
        print(f"  {dims} | visits={int(v)} | orders={int(p)}")


def main():
    print(f"[probe] окно {FROM} .. {TO}")

    print("\n── utm_source=ya_direct: источник по Метрике и кампания Директа (по клику) ──")
    fetch(
        ("ym:s:lastsignTrafficSource", "ym:s:lastsignSourceEngine",
         "ym:s:lastsignDirectClickOrder", "ym:s:lastsignDirectClickOrderName", "ym:s:lastsignUTMMedium"),
        "ym:s:lastsignUTMSource=='ya_direct'",
    )

    print("\n── utm_source=ya_direct: utm_campaign / utm_content ──")
    fetch(
        ("ym:s:lastsignUTMCampaign", "ym:s:lastsignUTMContent"),
        "ym:s:lastsignUTMSource=='ya_direct'",
        limit=30,
    )

    print("\n── Директ по клику (yclid) БЕЗ utm_campaign: сколько всего и какие кампании ──")
    fetch(
        ("ym:s:lastsignUTMSource", "ym:s:lastsignDirectClickOrder", "ym:s:lastsignDirectClickOrderName"),
        "ym:s:lastsignSourceEngine=='Яндекс: Директ' AND ym:s:lastsignUTMCampaign=='(not set)'",
    )

    print("\n── google/cpc: utm_campaign и что Метрика знает ──")
    fetch(
        ("ym:s:lastsignTrafficSource", "ym:s:lastsignSourceEngine", "ym:s:lastsignUTMCampaign",
         "ym:s:lastsignUTMContent"),
        "ym:s:lastsignUTMSource=='google' AND ym:s:lastsignUTMMedium=='cpc'",
        limit=30,
    )

    print("\n── ya.GO/cpm ──")
    fetch(
        ("ym:s:lastsignTrafficSource", "ym:s:lastsignSourceEngine", "ym:s:lastsignUTMCampaign",
         "ym:s:lastsignDirectClickOrderName"),
        "ym:s:lastsignUTMSource=='ya.GO'",
        limit=30,
    )


if __name__ == "__main__":
    main()
