# -*- coding: utf-8 -*-
"""Яндекс.Метрика Stat API — KZ-срез счётчика LIME (общий с RU).

KZ и RU живут на одном счётчике 23504302 и на одном домене limestore.com, поэтому
разделяем гео-страной визита (решение спеки 2026-07-18-lime-kz-metrika-design.md).
Проверено зондом: кросс измерений ниже не теряет ни визита против запроса «по дате»
(0.00% по всем метрикам), поэтому компенсация остатка, как в GCC, не нужна.
"""
import os
import time

import requests

# Повторы на транзиентные ошибки Stat API (см. fetch_kz_traffic). Пауза растёт линейно.
RETRIES = int(os.environ.get("LIME_KZ_METRIKA_RETRIES") or "3")
RETRY_SLEEP = int(os.environ.get("LIME_KZ_METRIKA_RETRY_SLEEP") or "5")

# Порядок важен только для нашего запроса: разбор читает позиции из эха ответа.
DIMENSIONS = (
    "ym:s:date",
    "ym:s:lastsignTrafficSource",
    "ym:s:lastsignSourceEngine",
    "ym:s:lastsignDirectClickOrderName",
    "ym:s:lastsignUTMCampaign",
    "ym:s:lastsignUTMContent",
)

# Цели счётчика 23504302: корзина и начало оформления (id из d:\vscode\LIME\config.py).
GOAL_CART = "194380276"
GOAL_CHECKOUT = "340817822"

# Порядок метрик задаём мы и читаем по индексу — менять только вместе с METRIC_FIELDS.
METRICS = (
    "ym:s:visits",
    "ym:s:users",
    "ym:s:newUsers",
    "ym:s:bounceRate",
    "ym:s:pageDepth",
    f"ym:s:goal{GOAL_CART}reaches",
    f"ym:s:goal{GOAL_CHECKOUT}reaches",
    "ym:s:ecommercePurchases",
    "ym:s:ecommerceRevenue",
)

METRIC_FIELDS = (
    "visits", "users", "new_users", "bounce_rate", "page_depth",
    "cart_reaches", "checkout_reaches", "orders", "revenue",
)

GEO_FILTER = "ym:s:regionCountryName=='Kazakhstan'"

API_URL = "https://api-metrika.yandex.net/stat/v1/data"


def parse_metrika_kz(resp: dict) -> list[dict]:
    """Разбор ответа Stat API в плоские строки.

    Позиции измерений читаются из `resp["query"]["dimensions"]` (API возвращает эхо запроса),
    поэтому добавление или перестановка измерения не ломает разбор.

    Args:
        resp: полный ответ API с ключами "query" и "data".

    Returns:
        Список дектов: измерения + метрики из METRIC_FIELDS (недостающие метрики = 0.0).
    """
    queried = (resp.get("query") or {}).get("dimensions") or []
    pos = {name: i for i, name in enumerate(queried)}

    def dim(dims: list, attr: str, field: str):
        i = pos.get(attr)
        if i is None or i >= len(dims):
            return None
        return (dims[i] or {}).get(field)

    rows = []
    for item in resp.get("data", []):
        dims = item.get("dimensions", [])
        metrics = item.get("metrics", []) or []
        row = {
            "date": dim(dims, "ym:s:date", "name"),
            "traffic_source": dim(dims, "ym:s:lastsignTrafficSource", "id"),
            "source_engine": dim(dims, "ym:s:lastsignSourceEngine", "name"),
            "direct_campaign_name": dim(dims, "ym:s:lastsignDirectClickOrderName", "name"),
            "utm_campaign": dim(dims, "ym:s:lastsignUTMCampaign", "name"),
            "utm_content": dim(dims, "ym:s:lastsignUTMContent", "name"),
        }
        for i, field in enumerate(METRIC_FIELDS):
            row[field] = float(metrics[i] or 0) if i < len(metrics) else 0.0
        rows.append(row)
    return rows


def fetch_kz_traffic(counter_id, token: str, date_from: str, date_to: str) -> list[dict]:
    """Забрать KZ-срез (гео Казахстан) за период.

    Повторяет запрос при неуспешном ответе: Stat API периодически отдаёт транзиентную
    ошибку на отдельной дате (2026-07-19: HTTP 400 на 2026-04-05, тот же запрос минутой
    позже — 200). Без повтора одна такая осечка роняет весь прогон, а для ежедневного
    синка это тихо пропущенный день.

    Args:
        counter_id: id счётчика (23504302).
        token: OAuth-токен Яндекса с доступом к счётчику.
        date_from, date_to: даты YYYY-MM-DD включительно.

    Returns:
        Строки parse_metrika_kz.

    Raises:
        requests.HTTPError: если все попытки вернули ошибку.
    """
    params = {
        "ids": counter_id,
        "date1": date_from,
        "date2": date_to,
        "metrics": ",".join(METRICS),
        "dimensions": ",".join(DIMENSIONS),
        "filters": GEO_FILTER,
        "accuracy": "full",
        "limit": 100000,
    }
    headers = {"Authorization": f"OAuth {token}"}

    resp = None
    for attempt in range(1, RETRIES + 1):
        resp = requests.get(API_URL, headers=headers, params=params, timeout=120)
        if resp.status_code == 200:
            return parse_metrika_kz(resp.json())
        if attempt < RETRIES:
            print(f"lime_kz_metrika_api: WARN {date_from} HTTP {resp.status_code}, "
                  f"попытка {attempt} из {RETRIES}, повтор через {RETRY_SLEEP * attempt}с")
            time.sleep(RETRY_SLEEP * attempt)

    resp.raise_for_status()
    return []  # недостижимо: raise_for_status уже бросил на не-200
