# -*- coding: utf-8 -*-
"""Зонд П2-b: каким параметром Triple Whale принимает фильтр по стране.

В UI (экран Attribution) есть селектор «All Countries» со списком ISO-2 кодов — значит
разбивка по странам у TW ЕСТЬ, вопрос только в имени параметра API. Прошлый зонд
(probe_tw_campaign_spend) ошибочно заключил «данных нет»: он проверил выдуманные имена
эндпоинтов и получил 404, из чего следует лишь «вход не найден».

Здесь угадывание заменено на проверяемый оракул: за Jan 1 – Jul 12 2026 по стране QA
интерфейс показывает Google 1 982,48 AED и Meta 10 543,76 AED. Кандидат считается
рабочим, только если ответ сходится с этими числами; ответ, равный безфильтровому,
означает, что параметр молча проигнорирован.

Только чтение. Запуск: python -m scripts.probe_tw_country_filter
"""
import io
import os
import sys
import time

import requests
from dotenv import load_dotenv

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
load_dotenv()

URL = "https://api.triplewhale.com/api/v2/summary-page/get-data"
START, END = "2026-01-01", "2026-07-12"

# Эталон из интерфейса (скриншот Павла, страна QA, тот же период).
EXPECT_GOOGLE = 1982.48
EXPECT_META = 10543.76
TOLERANCE = 0.02  # 2% — на случай округлений и часового пояса


def call(body: dict, attempts: int = 3):
    """TW рвёт соединение на частых запросах — повторяем с паузой, иначе зонд
    падает на середине и половина кандидатов остаётся непроверенной."""
    last = ""
    for attempt in range(1, attempts + 1):
        try:
            r = requests.post(
                URL,
                headers={"x-api-key": os.environ["GCC_TRIPLEWHALE_API_KEY"],
                         "content-type": "application/json"},
                json=body, timeout=180,
            )
        except Exception as exc:
            last = f"{type(exc).__name__}"
            time.sleep(5 * attempt)
            continue
        if r.status_code != 200:
            return None, f"HTTP {r.status_code} {(r.text or '')[:80]}"
        metrics = {m.get("metricId"): (m.get("values") or {}).get("current")
                   for m in (r.json().get("metrics") or [])}
        return metrics, None
    return None, f"сеть: {last}"


def main():
    shop = os.environ["GCC_TW_SHOP_DOMAIN"]
    base = {"shopDomain": shop, "period": {"start": START, "end": END}, "todayHour": 25}

    baseline, err = call(base)
    if err:
        print("базовый вызов не прошёл:", err)
        return
    b_g = baseline.get("ga_adCost") or 0
    b_m = baseline.get("fb_ads_spend") or 0
    print(f"без фильтра:  Google {b_g:,.2f}  Meta {b_m:,.2f}")
    print(f"ожидаем (QA): Google {EXPECT_GOOGLE:,.2f}  Meta {EXPECT_META:,.2f}\n")

    candidates = {
        "country": {"country": "QA"},
        "countries": {"countries": ["QA"]},
        "countryCode": {"countryCode": "QA"},
        "geo": {"geo": "QA"},
        "market": {"market": "QA"},
        "filters.list": {"filters": [{"key": "country", "value": ["QA"]}]},
        "filters.dict": {"filters": {"country": ["QA"]}},
        "filter.dict": {"filter": {"country": ["QA"]}},
        "segment": {"segment": {"country": "QA"}},
        "breakdown+value": {"breakdown": "country", "breakdownValue": "QA"},
        "attributionFilters": {"attributionFilters": [{"country": "QA"}]},
        "customFilters": {"customFilters": [{"key": "country", "value": ["QA"]}]},
        "period.country": {"period": {"start": START, "end": END, "country": "QA"}},
    }

    hits = []
    for name, extra in candidates.items():
        time.sleep(3)
        body = {**base, **extra}
        metrics, err = call(body)
        if err:
            print(f"  {name:<22} {err}")
            continue
        g = metrics.get("ga_adCost") or 0
        m = metrics.get("fb_ads_spend") or 0
        if abs(g - b_g) < 0.01 and abs(m - b_m) < 0.01:
            print(f"  {name:<22} проигнорирован (= без фильтра)")
            continue
        ok_g = abs(g - EXPECT_GOOGLE) <= EXPECT_GOOGLE * TOLERANCE
        ok_m = abs(m - EXPECT_META) <= EXPECT_META * TOLERANCE
        mark = "✓ СОШЛОСЬ" if (ok_g and ok_m) else "изменил, но не сошёлся"
        print(f"  {name:<22} Google {g:,.2f}  Meta {m:,.2f}   {mark}")
        if ok_g and ok_m:
            hits.append(name)

    print("\nрабочие формы:", hits or "ни одна")


if __name__ == "__main__":
    main()
