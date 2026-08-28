# Проба BJORN, раунд 10. Раунд 9 закрыл матчинг заголовков (99,3%) и productID, но вскрыл
# расхождение и уронил Logs API на моей опечатке в адресе (создание запроса идёт на
# /logrequests, а статус и выгрузка — на /logrequest/{id}).
# Здесь:
#   1. Почему 246 товаров из 343 имеют клики, но ноль показов и ноль просмотров карточки:
#      смотрю все четыре ступени в одной выборке вместе с productID.
#   2. Все ли карточки товара живут под /product/ — если нет, фильтр Logs API потеряет часть.
#   3. Рабочая сшивка хитов и визитов по visitID за один день + формат объявления в логах.
from __future__ import annotations

import os
import time
from collections import Counter
from typing import Any

import requests

STAT = "https://api-metrika.yandex.net/stat/v1/data"
BASE = "https://api-metrika.yandex.net/management/v1/counter/{counter}"

DATE1 = os.environ.get("PROBE_DATE1", "2026-08-01")
DATE2 = os.environ.get("PROBE_DATE2", "2026-08-27")
LOG_DAY = os.environ.get("PROBE_LOG_DAY", "2026-08-20")

TOKEN = os.environ["METRICA_TOKEN"]
COUNTER = os.environ["METRICA_COUNTER_ID"]
HEADERS = {"Authorization": f"OAuth {TOKEN}"}


def stat(params: dict[str, Any]) -> dict[str, Any]:
    resp = requests.get(STAT, params=params, headers=HEADERS, timeout=180)
    if resp.status_code != 200:
        return {"_error": resp.text[:300]}
    return resp.json()


def probe_steps_together() -> None:
    print("=" * 78)
    print(f"1. Четыре ступени в одной выборке + productID · {DATE1}..{DATE2}")
    print("=" * 78)
    base = {
        "ids": COUNTER, "date1": DATE1, "date2": DATE2, "accuracy": "full",
        "proposed_accuracy": "false", "lang": "ru", "limit": 100000, "attribution": "lastsign",
        "metrics": "ym:s:productImpressions,ym:s:productClicks,ym:s:productBasketsQuantity,ym:s:productPurchasedQuantity",
    }

    for dims in ("ym:s:productName", "ym:s:productID,ym:s:productName"):
        body = stat({**base, "dimensions": dims})
        if "_error" in body:
            print(f"  {dims}: ОШИБКА {body['_error']}")
            continue
        rows = body.get("data", [])
        print(f"\n  группировка {dims}: строк={len(rows)} totals={body.get('totals')}")
        only_clicks = [d for d in rows if d["metrics"][0] == 0 and d["metrics"][1] > 0]
        print(
            f"    из них «клики без показов»: {len(only_clicks)}"
            f" (кликов {sum(d['metrics'][1] for d in only_clicks):.0f},"
            f" покупок {sum(d['metrics'][3] for d in only_clicks):.0f})"
        )
        for d in sorted(rows, key=lambda x: -x["metrics"][1])[:6]:
            names = [str(x.get("name"))[:44] for x in d["dimensions"]]
            print(f"    {d['metrics']}  {names}")


def probe_card_urls() -> None:
    print("\n" + "=" * 78)
    print("2. Где живут карточки товара: разделы сайта по просмотрам")
    print("=" * 78)
    base = {
        "ids": COUNTER, "date1": DATE1, "date2": DATE2, "accuracy": "full",
        "proposed_accuracy": "false", "lang": "ru", "limit": 25,
        "metrics": "ym:pv:pageviews,ym:pv:users",
    }
    body = stat({**base, "dimensions": "ym:pv:URLPathLevel1"})
    print("  верхний уровень адресов:")
    for d in body.get("data", []):
        print(f"    {d['metrics'][0]:>8.0f} {d['metrics'][1]:>7.0f}  {str(d['dimensions'][0].get('name'))[:64]}")

    body = stat({
        **base, "limit": 12, "dimensions": "ym:pv:URLPathLevel2",
        "filters": "ym:pv:URLPathLevel1=='https://bjornlarsen.ru/catalog/'",
    })
    print("\n  внутри /catalog/ (вдруг карточки лежат там же):")
    for d in body.get("data", []):
        print(f"    {d['metrics'][0]:>8.0f} {d['metrics'][1]:>7.0f}  {str(d['dimensions'][0].get('name'))[:64]}")


def logs_create(source: str, fields: str) -> int | None:
    resp = requests.post(
        BASE.format(counter=COUNTER) + "/logrequests",
        params={"date1": LOG_DAY, "date2": LOG_DAY, "fields": fields, "source": source},
        headers=HEADERS,
        timeout=120,
    )
    if resp.status_code != 200:
        print(f"    создание {source}: HTTP {resp.status_code} {resp.text[:250]}")
        return None
    return resp.json()["log_request"]["request_id"]


def logs_wait(request_id: int, tries: int = 40) -> dict | None:
    for _ in range(tries):
        resp = requests.get(BASE.format(counter=COUNTER) + f"/logrequest/{request_id}", headers=HEADERS, timeout=120)
        if resp.status_code != 200:
            print(f"    статус: HTTP {resp.status_code} {resp.text[:200]}")
            return None
        info = resp.json()["log_request"]
        if info["status"] == "processed":
            return info
        if info["status"] in ("processing_failed", "canceled"):
            print(f"    статус {info['status']}")
            return None
        time.sleep(15)
    print("    не дождались подготовки лога")
    return None


def logs_download(request_id: int, part: int) -> str:
    resp = requests.get(
        BASE.format(counter=COUNTER) + f"/logrequest/{request_id}/part/{part}/download",
        headers=HEADERS,
        timeout=600,
    )
    return resp.text if resp.status_code == 200 else ""


def logs_clean(request_id: int) -> None:
    resp = requests.post(BASE.format(counter=COUNTER) + f"/logrequest/{request_id}/clean", headers=HEADERS, timeout=120)
    print(f"    clean {request_id}: HTTP {resp.status_code}")


def probe_logs_join() -> None:
    print("\n" + "=" * 78)
    print(f"3. Сшивка хитов и визитов по visitID · день {LOG_DAY}")
    print("=" * 78)

    hits_id = logs_create("hits", "ym:pv:visitID,ym:pv:dateTime,ym:pv:URL,ym:pv:title")
    visits_id = logs_create(
        "visits",
        "ym:s:visitID,ym:s:lastTrafficSource,ym:s:lastUTMCampaign,ym:s:lastUTMContent,ym:s:lastDirectClickBanner",
    )
    if not hits_id or not visits_id:
        for rid in (hits_id, visits_id):
            if rid:
                logs_clean(rid)
        return
    print(f"  запросы созданы: хиты={hits_id} визиты={visits_id}")

    t0 = time.time()
    hits_info = logs_wait(hits_id)
    visits_info = logs_wait(visits_id)
    print(f"  подготовка заняла {time.time() - t0:.0f} c")
    if not hits_info or not visits_info:
        for rid in (hits_id, visits_id):
            logs_clean(rid)
        return

    try:
        print(f"  частей: хиты={len(hits_info.get('parts', []))} визиты={len(visits_info.get('parts', []))}")
        hits_text = "".join(logs_download(hits_id, p["part_number"]) for p in hits_info.get("parts", []))
        visits_text = "".join(logs_download(visits_id, p["part_number"]) for p in visits_info.get("parts", []))

        visit_src: dict[str, list[str]] = {}
        for i, line in enumerate(visits_text.splitlines()):
            if i == 0:
                print(f"  шапка визитов: {line}")
                continue
            p = line.split("\t")
            if len(p) >= 5:
                visit_src[p[0]] = p[1:5]

        card_hits = joined = 0
        by_source: Counter[str] = Counter()
        banners: Counter[str] = Counter()
        contents: Counter[str] = Counter()
        for i, line in enumerate(hits_text.splitlines()):
            if i == 0:
                print(f"  шапка хитов:   {line}")
                continue
            p = line.split("\t")
            if len(p) < 4 or "/product/" not in p[2]:
                continue
            card_hits += 1
            src = visit_src.get(p[0])
            if src:
                joined += 1
                by_source[src[0]] += 1
                if src[3]:
                    banners[src[3]] += 1
                if src[2]:
                    contents[src[2]] += 1

        print(f"\n  строк: хиты={len(hits_text.splitlines())} визиты={len(visit_src)}")
        print(f"  просмотров карточек /product/: {card_hits}")
        if card_hits:
            print(f"  сшилось с визитом: {joined} ({joined / card_hits * 100:.1f}%)")
        print(f"  источники: {by_source.most_common(8)}")
        print(f"  lastDirectClickBanner: {banners.most_common(5)}")
        print(f"  lastUTMContent: {contents.most_common(5)}")
    finally:
        for rid in (hits_id, visits_id):
            logs_clean(rid)


if __name__ == "__main__":
    probe_steps_together()
    probe_card_urls()
    probe_logs_join()
