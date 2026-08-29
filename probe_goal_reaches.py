#!/usr/bin/env python3
"""Почему цель «Регистрация» (497698909) в витрине даёт единицы, а в Метрике — сотни.

Интерфейс Метрики за 17–23.08.2026 показывает 414 достижений, витрина
lime_metrika_goal_daily — 3. Скрипт спрашивает Stat API четырьмя способами и кладёт
ответы рядом, чтобы стало видно, что именно теряет синк: разрез, гео-фильтр,
пагинацию или пачку метрик.

Только чтение: GET в Stat API, ни одной записи.
"""
import os

import requests

STAT_URL = "https://api-metrika.yandex.net/stat/v1/data"
COUNTER = os.environ.get("LIME_METRIKA_COUNTER_ID") or "23504302"
TOKEN = os.environ["LIME_METRIKA_TOKEN"]
GOAL = os.environ.get("PROBE_GOAL") or "497698909"
FROM = os.environ.get("PROBE_FROM") or "2026-08-17"
TO = os.environ.get("PROBE_TO") or "2026-08-23"

DIMENSIONS = (
    "ym:s:date",
    "ym:s:lastsignTrafficSource",
    "ym:s:lastsignSourceEngine",
    "ym:s:lastsignDirectClickOrderName",
    "ym:s:lastsignUTMCampaign",
)
GEO = "ym:s:regionCountryName=='Russia'"


def get(params: dict) -> dict:
    r = requests.get(
        STAT_URL,
        headers={"Authorization": f"OAuth {TOKEN}"},
        params={"ids": COUNTER, "date1": FROM, "date2": TO, "accuracy": "full", **params},
        timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:400]}")
    return r.json()


def total_of(resp: dict) -> float:
    tot = resp.get("totals") or []
    return float(tot[0]) if tot else 0.0


def sum_rows(resp: dict) -> float:
    return sum(float((it.get("metrics") or [0])[0] or 0) for it in resp.get("data", []))


def show(label: str, params: dict, limit: int = 100000) -> dict:
    resp = get({**params, "limit": limit})
    print(
        f"  {label:<52} totals {total_of(resp):>10,.0f} | "
        f"сумма строк {sum_rows(resp):>10,.0f} | строк {len(resp.get('data', [])):>6} "
        f"из {int(resp.get('total_rows') or 0):>7}"
    )
    return resp


def main():
    print(f"[probe] цель {GOAL}, окно {FROM}..{TO}, счётчик {COUNTER}\n")
    m = f"ym:s:goal{GOAL}reaches"

    print("=== 1. Одна цель, разные условия ===")
    show("только по дате, без гео-фильтра", {"metrics": m, "dimensions": "ym:s:date"})
    show("только по дате + гео Russia", {"metrics": m, "dimensions": "ym:s:date", "filters": GEO})
    show("полный разрез синка + гео", {
        "metrics": m, "dimensions": ",".join(DIMENSIONS), "filters": GEO})
    show("полный разрез, без гео", {"metrics": m, "dimensions": ",".join(DIMENSIONS)})

    print("\n=== 2. Атрибуция: lastsign против других ===")
    for attr in ("lastsign", "last", "first", "cross_device_last_significant"):
        try:
            show(f"attribution={attr}, только дата", {
                "metrics": m, "dimensions": "ym:s:date", "attribution": attr})
        except RuntimeError as e:
            print(f"  attribution={attr}: {e}")

    print("\n=== 3. Пачка целей, как в синке (цель НЕ первая в пачке) ===")
    # Синк тянет по 18 целей за запрос и сортирует ответ Метрика по первой метрике.
    # Если хвост обрезается, цель из середины пачки теряет достижения.
    others = ["340818283", "567880168", "569296987", "515616045"]
    metrics = ",".join(f"ym:s:goal{g}reaches" for g in [*others, GOAL])
    resp = get({"metrics": metrics, "dimensions": ",".join(DIMENSIONS),
                "filters": GEO, "limit": 100000})
    idx = len(others)
    got = sum(float((it.get("metrics") or [])[idx] or 0) for it in resp.get("data", []))
    tot = (resp.get("totals") or [])
    print(f"  цель {GOAL} 5-й метрикой в пачке: сумма строк {got:,.0f} | "
          f"totals {float(tot[idx]) if len(tot) > idx else 0:,.0f} | "
          f"строк {len(resp.get('data', []))} из {int(resp.get('total_rows') or 0)}")

    print("\n=== 4. Где именно живут достижения (топ разрезов, без гео) ===")
    resp = get({"metrics": m, "dimensions": ",".join(DIMENSIONS), "limit": 50,
                "sort": f"-{m}"})
    for it in resp.get("data", [])[:20]:
        dims = " / ".join(str((d or {}).get("name") or (d or {}).get("id") or "—")[:22]
                          for d in it.get("dimensions", []))
        print(f"  {dims:<110} {float((it.get('metrics') or [0])[0] or 0):>8,.0f}")


    print("\n=== 5. По стране визита (кто именно достигает цель) ===")
    resp = get({"metrics": m, "dimensions": "ym:s:regionCountryName",
                "limit": 30, "sort": f"-{m}"})
    for it in resp.get("data", [])[:15]:
        name = ((it.get("dimensions") or [{}])[0] or {}).get("name") or "—"
        print(f"  {str(name)[:40]:<42} {float((it.get('metrics') or [0])[0] or 0):>8,.0f}")

    print("\n=== 6. Та же разбивка для «Регистрация или авторизация» 340818283 ===")
    m2 = "ym:s:goal340818283reaches"
    resp = get({"metrics": m2, "dimensions": "ym:s:regionCountryName",
                "limit": 30, "sort": f"-{m2}"})
    for it in resp.get("data", [])[:10]:
        name = ((it.get("dimensions") or [{}])[0] or {}).get("name") or "—"
        print(f"  {str(name)[:40]:<42} {float((it.get('metrics') or [0])[0] or 0):>8,.0f}")


if __name__ == "__main__":
    main()
