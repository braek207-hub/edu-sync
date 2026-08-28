# Проба BJORN, раунд 8. Павел подтвердил: ступень «просмотр карточки товара» нужна.
# Единственный путь — Logs API (сшивка хитов и визитов по visitID). Закрываю последнее:
#   1. Какие поля хитов доступны — в первую очередь title (заголовок страницы). Если он есть,
#      имя товара берётся прямо из хита и карта «адрес → товар» не нужна вовсе.
#   2. Какие поля визитов доступны для разреза (источник, кампания, объявление).
#   3. Как выглядят адреса и заголовки карточек /product/ — на чём строить разбор.
#   4. Вес выгрузки обоих источников за месяц.
# Read-only: только evaluate (запрос лога не создаётся) и stat API.
from __future__ import annotations

import os
import time

import requests

LOGS = "https://api-metrika.yandex.net/management/v1/counter/{counter}/logrequests"
STAT = "https://api-metrika.yandex.net/stat/v1/data"
DATE1 = os.environ.get("PROBE_DATE1", "2026-08-01")
DATE2 = os.environ.get("PROBE_DATE2", "2026-08-27")


def evaluate(counter: str, headers: dict, fields: str, source: str) -> tuple[bool, str]:
    resp = requests.get(
        LOGS.format(counter=counter) + "/evaluate",
        params={"date1": DATE1, "date2": DATE2, "fields": fields, "source": source},
        headers=headers,
        timeout=120,
    )
    return resp.status_code == 200, resp.text


def main() -> None:
    token = os.environ["METRICA_TOKEN"]
    counter = os.environ["METRICA_COUNTER_ID"]
    headers = {"Authorization": f"OAuth {token}"}

    print("=" * 78)
    print(f"1. Поля ХИТОВ в Logs API · {DATE1}..{DATE2}")
    print("=" * 78)
    hit_fields = [
        "ym:pv:watchID", "ym:pv:visitID", "ym:pv:counterUserIDHash", "ym:pv:dateTime",
        "ym:pv:title", "ym:pv:URL", "ym:pv:referer", "ym:pv:clientID",
        "ym:pv:lastTrafficSource", "ym:pv:UTMCampaign", "ym:pv:UTMContent",
        "ym:pv:isPageView", "ym:pv:deviceCategory", "ym:pv:params",
    ]
    ok_hits: list[str] = []
    for f in hit_fields:
        ok, text = evaluate(counter, headers, f"ym:pv:watchID,{f}", "hits")
        print(f"  {'OK ' if ok else 'нет'} {f:<32} {'' if ok else text[:100]}")
        if ok:
            ok_hits.append(f)
        time.sleep(0.25)

    print("\n" + "=" * 78)
    print("2. Поля ВИЗИТОВ для разреза")
    print("=" * 78)
    visit_fields = [
        "ym:s:visitID", "ym:s:dateTime", "ym:s:clientID",
        "ym:s:lastTrafficSource", "ym:s:lastSourceEngine", "ym:s:lastAdvEngine",
        "ym:s:lastUTMSource", "ym:s:lastUTMCampaign", "ym:s:lastUTMContent",
        "ym:s:lastDirectClickBanner", "ym:s:lastDirectBannerGroup", "ym:s:lastDirectClickOrder",
        "ym:s:purchaseID", "ym:s:productsName", "ym:s:productsID", "ym:s:productsQuantity",
        "ym:s:productsPrice", "ym:s:productsEventTime", "ym:s:productsList",
    ]
    ok_visits: list[str] = []
    for f in visit_fields:
        ok, text = evaluate(counter, headers, f"ym:s:visitID,{f}", "visits")
        print(f"  {'OK ' if ok else 'нет'} {f:<32} {'' if ok else text[:100]}")
        if ok:
            ok_visits.append(f)
        time.sleep(0.25)

    print("\n" + "=" * 78)
    print("3. Как выглядят карточки товара: адрес и заголовок")
    print("=" * 78)
    base = {
        "ids": counter, "date1": DATE1, "date2": DATE2, "accuracy": "full",
        "proposed_accuracy": "false", "lang": "ru", "limit": 8,
    }
    for dim in ("ym:pv:URL", "ym:pv:title"):
        resp = requests.get(
            STAT,
            params={
                **base,
                "metrics": "ym:pv:pageviews,ym:pv:users",
                "dimensions": dim,
                "filters": "ym:pv:URLPathLevel2=='https://bjornlarsen.ru/product/'",
            },
            headers=headers,
            timeout=120,
        )
        body = resp.json() if resp.status_code == 200 else {}
        print(f"\n  {dim}: HTTP {resp.status_code} строк={body.get('total_rows')}")
        for d in (body.get("data") or [])[:8]:
            print("   ", [str(x.get("name"))[:78] for x in d.get("dimensions", [])], d.get("metrics"))

    print("\n" + "=" * 78)
    print("4. Вес выгрузки за месяц")
    print("=" * 78)
    hits_pick = "ym:pv:watchID,ym:pv:visitID,ym:pv:dateTime,ym:pv:URL,ym:pv:title"
    ok, text = evaluate(counter, headers, hits_pick, "hits")
    print(f"  хиты  ({hits_pick}):\n    {text[:400]}")
    visits_pick = ",".join(f for f in ok_visits if f in {
        "ym:s:visitID", "ym:s:dateTime", "ym:s:lastTrafficSource",
        "ym:s:lastUTMCampaign", "ym:s:lastUTMContent", "ym:s:lastDirectClickBanner",
    })
    ok, text = evaluate(counter, headers, visits_pick, "visits")
    print(f"  визиты ({visits_pick}):\n    {text[:400]}")


if __name__ == "__main__":
    main()
