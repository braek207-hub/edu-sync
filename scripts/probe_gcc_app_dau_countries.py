# -*- coding: utf-8 -*-
"""Зонд: DAU приложения 6299245 за W33 по ВСЕМ странам — откуда 4 183 в ручном отчёте.

Гипотеза: наш app-срез = 5 стран Залива (1 583/нед), а агентство берёт DAU всего
приложения без фильтра страны. Печатаем полное распределение по regionCountry.
Запуск: python scripts/probe_gcc_app_dau_countries.py (нужен APPMETRICA_TOKEN)
"""
import io
import json
import os
import sys
import urllib.parse
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

FRM, TO = "2026-08-10", "2026-08-16"
APP_ID = os.environ.get("GCC_APP_ID") or "6299245"


def main() -> None:
    params = {"id": APP_ID, "date1": FRM, "date2": TO, "accuracy": "1",
              "limit": "10000", "metrics": "ym:s:devices",
              "dimensions": "ym:s:date,ym:s:regionCountry"}
    url = f"https://api.appmetrica.yandex.ru/stat/v1/data?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"OAuth {os.environ['APPMETRICA_TOKEN']}"})
    with urllib.request.urlopen(req, timeout=90) as r:
        data = json.loads(r.read().decode("utf-8"))

    by_country: dict[str, int] = {}
    total = 0
    for row in data.get("data", []):
        dims = [d.get("name") for d in row.get("dimensions", [])]
        if len(dims) < 2:
            continue
        dev = int(round(row["metrics"][0]))
        by_country[dims[1] or "∅"] = by_country.get(dims[1] or "∅", 0) + dev
        total += dev
    gcc5 = ("Объединённые Арабские Эмираты", "Саудовская Аравия", "Катар", "Кувейт", "Оман")
    print(f"W33 {FRM}..{TO}, app {APP_ID}: сумма дневных DAU по ВСЕМ странам = {total}")
    print(f"Залив-5 = {sum(by_country.get(c, 0) for c in gcc5)} | ручной 'Organic traffic APP' = 4183")
    print("\nтоп стран:")
    for c, v in sorted(by_country.items(), key=lambda kv: -kv[1])[:15]:
        print(f"  {c}: {v}")


if __name__ == "__main__":
    main()
