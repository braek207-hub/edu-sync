# -*- coding: utf-8 -*-
"""Зонд П4: можно ли вернуть площадку платному трафику GCC, не потеряв визиты.

Контекст: `lastsignSourceEngine` убран из прод-набора намеренно — в кроссе с доменом он
выбрасывает мелкие комбинации (Бахрейн терял 42% визитов). Площадка восстанавливается
только из utm, а 23% платных визитов приходят без меток вовсе и висят подканалом «Ad».

План 2026-07-18 предлагал отдельный запрос с движком, ОГРАНИЧЕННЫЙ рекламой: объём мал,
обрезка не должна срабатывать. Это надо доказать замером ДО реализации.

Печатает только агрегаты. Запуск: python -m scripts.probe_gcc_ad_engine [дней]
"""
import io
import os
import sys
from collections import defaultdict
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sync.gcc_channels import map_domain_country  # noqa: E402

COUNTER = os.environ.get("GCC_METRICA_COUNTER_ID") or "98232701"


def fetch(token, date1, date2, dimensions, filters=None):
    params = {
        "ids": COUNTER, "date1": date1, "date2": date2,
        "metrics": "ym:s:visits", "dimensions": ",".join(dimensions),
        "accuracy": "full", "limit": 100000,
    }
    if filters:
        params["filters"] = filters
    r = requests.get(
        "https://api-metrika.yandex.net/stat/v1/data",
        headers={"Authorization": f"OAuth {token}"}, params=params, timeout=60,
    )
    r.raise_for_status()
    return r.json()


def total(resp):
    return sum(row["metrics"][0] for row in resp.get("data", []))


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    token = os.environ["GCC_METRICA_TOKEN"]
    to = date.today() - timedelta(days=1)
    frm = to - timedelta(days=days - 1)
    d1, d2 = frm.isoformat(), to.isoformat()
    AD = "ym:s:lastsignTrafficSource=='ad'"

    print(f"[зонд П4] {d1}…{d2}\n")

    a = fetch(token, d1, d2, ["ym:s:date", "ym:s:lastsignTrafficSource"], AD)
    b = fetch(token, d1, d2, ["ym:s:date", "ym:s:startURLDomain",
                              "ym:s:lastsignTrafficSource"], AD)
    c = fetch(token, d1, d2, ["ym:s:date", "ym:s:startURLDomain",
                              "ym:s:lastsignTrafficSource",
                              "ym:s:lastsignSourceEngine"], AD)

    ta, tb, tc = total(a), total(b), total(c)
    print("[1] Обрезка при добавлении измерений (фильтр: только реклама)")
    print(f"    дата+источник                      {ta:>8.0f}  (эталон)")
    print(f"    +домен                             {tb:>8.0f}  {100*(tb-ta)/ta:+.2f}%")
    print(f"    +домен+ДВИЖОК                      {tc:>8.0f}  {100*(tc-ta)/ta:+.2f}%")
    verdict = "БЕЗОПАСНО" if abs(tc - ta) / ta < 0.01 else "РЕЖЕТ — нужен остаток"
    print(f"    вердикт: {verdict}\n")

    # Насколько однозначна площадка внутри (дата, страна): если бакет одномоторный,
    # безметочный визит атрибутируется без домыслов.
    buckets = defaultdict(lambda: defaultdict(float))
    for row in c.get("data", []):
        dims = row["dimensions"]
        day = dims[0].get("name")
        country = map_domain_country(dims[1].get("name")) or "(вне GCC)"
        engine = dims[3].get("name") or "(без движка)"
        buckets[(day, country)][engine] += row["metrics"][0]

    single = multi = 0
    single_v = multi_v = 0.0
    for _, engines in buckets.items():
        v = sum(engines.values())
        if len([e for e in engines if e != "(без движка)"]) <= 1:
            single += 1
            single_v += v
        else:
            multi += 1
            multi_v += v
    print("[2] Однозначность площадки внутри (дата, страна)")
    print(f"    бакетов с ОДНОЙ площадкой:   {single:>4}  визитов {single_v:>8.0f}")
    print(f"    бакетов со СМЕСЬЮ площадок:  {multi:>4}  визитов {multi_v:>8.0f}")

    # Можно ли получить площадку И кампанию одним запросом, или придётся жертвовать.
    d = fetch(token, d1, d2, ["ym:s:date", "ym:s:startURLDomain",
                              "ym:s:lastsignTrafficSource", "ym:s:lastsignSourceEngine",
                              "ym:s:UTMCampaign"], AD)
    e = fetch(token, d1, d2, ["ym:s:date", "ym:s:startURLDomain",
                              "ym:s:lastsignTrafficSource", "ym:s:lastsignSourceEngine",
                              "ym:s:UTMSource", "ym:s:UTMCampaign"], AD)
    td, te = total(d), total(e)
    print("\n[1b] Добавляем кампанию к движку (тот же фильтр)")
    print(f"    +движок+кампания                   {td:>8.0f}  {100*(td-ta)/ta:+.2f}%")
    print(f"    +движок+utm_source+кампания        {te:>8.0f}  {100*(te-ta)/ta:+.2f}%")

    print("\n[3] Распределение платных визитов по площадкам (факт из движка)")
    by_engine = defaultdict(float)
    for row in c.get("data", []):
        by_engine[row["dimensions"][3].get("name") or "(без движка)"] += row["metrics"][0]
    for engine, v in sorted(by_engine.items(), key=lambda kv: -kv[1]):
        print(f"    {engine:<34} {v:>8.0f}  {100*v/tc:>5.1f}%")


if __name__ == "__main__":
    main()
