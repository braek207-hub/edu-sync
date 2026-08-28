# Проба BJORN, раунд 9 — проверка допущений спеки, а не новых возможностей.
# В спеке записаны два непроверенных утверждения, каждое ломает витрину, если неверно:
#   1. «Имя товара берётся из ym:pv:title, карта не нужна» — а сколько заголовков карточек
#      реально совпадает с productName из ecommerce после отрезания хвоста?
#   2. «Сшиваем хиты и визиты по visitID» — проверено было только наличие полей (evaluate),
#      но не то, что сшивка на живых данных даёт непустой результат и что объявление в логах
#      выглядит так же, как AdId Директа.
# Плюс мелочи, на которых стоит ключ витрины: заполненность productID, работа товарных
# метрик на всех трёх моделях атрибуции.
# Logs API здесь СОЗДАЁТ запрос лога (единственное не-read-only действие пробы) и убирает
# его за собой: без выгрузки сшивку не проверить.
from __future__ import annotations

import os
import re
import time
from collections import Counter
from typing import Any

import requests

STAT = "https://api-metrika.yandex.net/stat/v1/data"
LOGS = "https://api-metrika.yandex.net/management/v1/counter/{counter}/logrequest"
LOGS_LIST = "https://api-metrika.yandex.net/management/v1/counter/{counter}/logrequests"

DATE1 = os.environ.get("PROBE_DATE1", "2026-08-01")
DATE2 = os.environ.get("PROBE_DATE2", "2026-08-27")
# День для выгрузки логов — один, этого хватает для проверки сшивки.
LOG_DAY = os.environ.get("PROBE_LOG_DAY", "2026-08-20")

TOKEN = os.environ["METRICA_TOKEN"]
COUNTER = os.environ["METRICA_COUNTER_ID"]
HEADERS = {"Authorization": f"OAuth {TOKEN}"}


def stat(params: dict[str, Any]) -> dict[str, Any]:
    resp = requests.get(STAT, params=params, headers=HEADERS, timeout=180)
    if resp.status_code != 200:
        return {"_error": resp.text[:200]}
    return resp.json()


def norm(name: str) -> str:
    """Нормализация имени товара: режем маркетинговый хвост, схлопываем пробелы, регистр."""
    s = re.split(r"\s+[-–—]\s+купить", name, flags=re.IGNORECASE)[0]
    s = s.replace("ё", "е").lower()
    return re.sub(r"\s+", " ", s).strip()


def probe_title_match() -> None:
    print("=" * 78)
    print(f"1. Матчинг «заголовок карточки» → «товар ecommerce» · {DATE1}..{DATE2}")
    print("=" * 78)

    base = {
        "ids": COUNTER, "date1": DATE1, "date2": DATE2, "accuracy": "full",
        "proposed_accuracy": "false", "lang": "ru", "limit": 100000,
    }

    titles: dict[str, float] = {}
    body = stat({
        **base,
        "metrics": "ym:pv:pageviews",
        "dimensions": "ym:pv:title",
        "filters": "ym:pv:URLPathLevel2=='https://bjornlarsen.ru/product/'",
    })
    for d in body.get("data", []):
        titles[str(d["dimensions"][0]["name"])] = d["metrics"][0]

    products: dict[str, list[float]] = {}
    body = stat({
        **base,
        "attribution": "lastsign",
        "metrics": "ym:s:productImpressions,ym:s:productClicks,ym:s:productPurchasedQuantity",
        "dimensions": "ym:s:productName",
    })
    for d in body.get("data", []):
        products[str(d["dimensions"][0]["name"])] = d["metrics"]

    print(f"  заголовков карточек: {len(titles)}")
    print(f"  товаров в ecommerce: {len(products)}")

    prod_norm = {norm(p): p for p in products if p and p != "null"}
    matched, unmatched = [], []
    for t, views in titles.items():
        if not t or t == "null":
            continue
        (matched if norm(t) in prod_norm else unmatched).append((t, views))

    total_views = sum(v for _, v in matched) + sum(v for _, v in unmatched)
    m_views = sum(v for _, v in matched)
    print(f"\n  сматчилось заголовков: {len(matched)} из {len(matched) + len(unmatched)}")
    print(f"  доля просмотров, которая ложится на товар: {m_views / total_views * 100:.1f}% ({m_views:.0f} из {total_views:.0f})")

    print("\n  НЕ сматчились (топ-12 по просмотрам):")
    for t, v in sorted(unmatched, key=lambda x: -x[1])[:12]:
        print(f"    {v:>7.0f}  {t[:72]}")

    print("\n  товары ecommerce БЕЗ просмотров карточки (топ-8 по показам):")
    title_norm = {norm(t) for t in titles}
    no_view = [(p, m) for p, m in products.items() if p and p != "null" and norm(p) not in title_norm]
    for p, m in sorted(no_view, key=lambda x: -x[1][0])[:8]:
        print(f"    показы={m[0]:>7.0f} клики={m[1]:>6.0f} покупки={m[2]:>4.0f}  {p[:60]}")
    print(f"  всего таких товаров: {len(no_view)}")


def probe_product_id() -> None:
    print("\n" + "=" * 78)
    print("2. Заполненность productID (на нём стоит ключ витрины)")
    print("=" * 78)
    body = stat({
        "ids": COUNTER, "date1": DATE1, "date2": DATE2, "accuracy": "full",
        "proposed_accuracy": "false", "lang": "ru", "limit": 100000,
        "attribution": "lastsign",
        "metrics": "ym:s:productImpressions,ym:s:productPurchasedQuantity",
        "dimensions": "ym:s:productID,ym:s:productName",
    })
    rows = body.get("data", [])
    empty_id = sum(1 for d in rows if not d["dimensions"][0].get("name") or d["dimensions"][0]["name"] in ("null", ""))
    print(f"  строк товар×id: {len(rows)}, из них с пустым productID: {empty_id}")
    names_per_id: dict[str, set[str]] = {}
    for d in rows:
        pid = str(d["dimensions"][0].get("name"))
        names_per_id.setdefault(pid, set()).add(str(d["dimensions"][1].get("name")))
    multi = {k: v for k, v in names_per_id.items() if len(v) > 1}
    print(f"  id, под которыми несколько разных названий: {len(multi)}")
    for k, v in list(multi.items())[:4]:
        print(f"    {k}: {[x[:40] for x in list(v)[:3]]}")


def probe_attributions() -> None:
    print("\n" + "=" * 78)
    print("3. Товарные метрики на всех трёх моделях атрибуции")
    print("=" * 78)
    for attr in ("automatic", "first", "lastsign"):
        body = stat({
            "ids": COUNTER, "date1": DATE1, "date2": DATE2, "accuracy": "full",
            "proposed_accuracy": "false", "lang": "ru", "limit": 1, "attribution": attr,
            "metrics": "ym:s:productImpressions,ym:s:productClicks,ym:s:productBasketsQuantity,ym:s:productPurchasedQuantity",
            "dimensions": f"ym:s:{attr}TrafficSource",
        })
        if "_error" in body:
            print(f"  {attr:<10} ОШИБКА {body['_error']}")
        else:
            print(f"  {attr:<10} totals={body.get('totals')} строк={body.get('total_rows')}")
        time.sleep(0.4)


def logs_create(source: str, fields: str) -> int | None:
    resp = requests.post(
        LOGS.format(counter=COUNTER),
        params={"date1": LOG_DAY, "date2": LOG_DAY, "fields": fields, "source": source},
        headers=HEADERS,
        timeout=120,
    )
    if resp.status_code != 200:
        print(f"    создание запроса {source}: HTTP {resp.status_code} {resp.text[:250]}")
        return None
    return resp.json()["log_request"]["request_id"]


def logs_wait(request_id: int, tries: int = 40) -> dict | None:
    for _ in range(tries):
        resp = requests.get(f"{LOGS.format(counter=COUNTER)}/{request_id}", headers=HEADERS, timeout=120)
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
        f"{LOGS.format(counter=COUNTER)}/{request_id}/part/{part}/download",
        headers=HEADERS,
        timeout=600,
    )
    return resp.text if resp.status_code == 200 else ""


def logs_clean(request_id: int) -> None:
    requests.post(f"{LOGS.format(counter=COUNTER)}/{request_id}/clean", headers=HEADERS, timeout=120)


def probe_logs_join() -> None:
    print("\n" + "=" * 78)
    print(f"4. Реальная сшивка хитов и визитов по visitID · день {LOG_DAY}")
    print("=" * 78)

    hits_id = logs_create("hits", "ym:pv:visitID,ym:pv:dateTime,ym:pv:URL,ym:pv:title")
    visits_id = logs_create("visits", "ym:s:visitID,ym:s:lastTrafficSource,ym:s:lastUTMCampaign,ym:s:lastUTMContent,ym:s:lastDirectClickBanner")
    if not hits_id or not visits_id:
        return
    print(f"  запросы созданы: хиты={hits_id} визиты={visits_id}, ждём подготовки")

    hits_info = logs_wait(hits_id)
    visits_info = logs_wait(visits_id)
    if not hits_info or not visits_info:
        for rid in (hits_id, visits_id):
            logs_clean(rid)
        return

    try:
        hits_text = "".join(logs_download(hits_id, p["part_number"]) for p in hits_info.get("parts", []))
        visits_text = "".join(logs_download(visits_id, p["part_number"]) for p in visits_info.get("parts", []))

        visit_src: dict[str, tuple[str, str, str, str]] = {}
        for i, line in enumerate(visits_text.splitlines()):
            if i == 0:
                print(f"  шапка визитов: {line}")
                continue
            p = line.split("\t")
            if len(p) >= 5:
                visit_src[p[0]] = (p[1], p[2], p[3], p[4])

        card_hits = 0
        joined = 0
        by_source: Counter[str] = Counter()
        banner_samples: Counter[str] = Counter()
        utm_content_samples: Counter[str] = Counter()
        titles_seen: Counter[str] = Counter()
        for i, line in enumerate(hits_text.splitlines()):
            if i == 0:
                print(f"  шапка хитов:   {line}")
                continue
            p = line.split("\t")
            if len(p) < 4:
                continue
            visit_id, _dt, url, title = p[0], p[1], p[2], p[3]
            if "/product/" not in url:
                continue
            card_hits += 1
            titles_seen[title] += 1
            src = visit_src.get(visit_id)
            if src:
                joined += 1
                by_source[src[0]] += 1
                if src[3]:
                    banner_samples[src[3]] += 1
                if src[2]:
                    utm_content_samples[src[2]] += 1

        print(f"\n  строк хитов={len(hits_text.splitlines())} визитов={len(visit_src)}")
        print(f"  просмотров карточек /product/: {card_hits}")
        print(f"  из них сшилось с визитом: {joined} ({joined / card_hits * 100:.1f}%)" if card_hits else "  карточек нет")
        print(f"  по источникам: {by_source.most_common(8)}")
        print(f"  lastDirectClickBanner (топ-5): {banner_samples.most_common(5)}")
        print(f"  lastUTMContent (топ-5): {utm_content_samples.most_common(5)}")
        print(f"  примеры заголовков карточек: {[t[:52] for t, _ in titles_seen.most_common(3)]}")
    finally:
        for rid in (hits_id, visits_id):
            logs_clean(rid)
        print("\n  запросы лога убраны (clean)")


if __name__ == "__main__":
    probe_title_match()
    probe_product_id()
    probe_attributions()
    probe_logs_join()
