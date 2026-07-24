# -*- coding: utf-8 -*-
"""Яндекс.Метрика Stat API — RU-срез счётчика LIME (общий с KZ, 23504302).

RU и KZ живут на одном счётчике и домене limestore.com, разделяем гео-страной визита
(KZ-модуль фильтрует Kazakhstan, здесь — Russia).

Назначение — обогатить основную RU-таблицу (витрина PROCONTEXT) ПОВЕДЕНИЕМ и POST-CLICK
воронкой Метрики по каналу/кампании. Набор метрик ШИРЕ KZ-среза (добавлены время на сайте
и цели «просмотр карточки»/«смотреть образ»), поэтому модуль самодостаточен, а не импортирует
METRICS у KZ. Разрез (DIMENSIONS) — общий. Post-view остаётся за Медиаметрикой.
"""
import os
import time

import requests

API_URL = "https://api-metrika.yandex.net/stat/v1/data"

RETRIES = int(os.environ.get("LIME_RU_METRIKA_RETRIES") or "3")
RETRY_SLEEP = int(os.environ.get("LIME_RU_METRIKA_RETRY_SLEEP") or "5")

# Порядок важен только для нашего запроса: разбор читает позиции измерений из эха ответа.
DIMENSIONS = (
    "ym:s:date",
    "ym:s:lastsignTrafficSource",
    "ym:s:lastsignSourceEngine",
    "ym:s:lastsignDirectClickOrderName",
    "ym:s:lastsignUTMCampaign",
    "ym:s:lastsignUTMContent",
)

# Цели счётчика 23504302 (id из d:\vscode\LIME\config.py METRIKA_GOALS).
GOAL_CARD = "340814310"      # просмотр карточки товара
GOAL_IMAGE = "340902369"     # смотреть образ
GOAL_CART = "194380276"      # добавление в корзину
GOAL_CHECKOUT = "340817822"  # начало оформления

# Порядок метрик задаём мы и читаем по индексу — менять только вместе с METRIC_FIELDS.
METRICS = (
    "ym:s:visits",
    "ym:s:users",
    "ym:s:newUsers",
    "ym:s:bounceRate",
    "ym:s:pageDepth",
    "ym:s:avgVisitDurationSeconds",
    f"ym:s:goal{GOAL_CARD}reaches",
    f"ym:s:goal{GOAL_IMAGE}reaches",
    f"ym:s:goal{GOAL_CART}reaches",
    f"ym:s:goal{GOAL_CHECKOUT}reaches",
    "ym:s:ecommercePurchases",
    "ym:s:ecommerceRevenue",
)

METRIC_FIELDS = (
    "visits", "users", "new_users", "bounce_rate", "page_depth", "avg_duration",
    "card_view", "look_image", "cart_reaches", "checkout_reaches", "orders", "revenue",
)

GEO_FILTER = "ym:s:regionCountryName=='Russia'"


def parse_ru(resp: dict) -> list[dict]:
    """Разбор ответа Stat API в плоские строки. Позиции измерений — из эха query."""
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


def fetch_ru_traffic(counter_id, token: str, date_from: str, date_to: str) -> list[dict]:
    """Забрать RU-срез (гео Россия) за период. Повторяет запрос на транзиентной ошибке."""
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
            return parse_ru(resp.json())
        if attempt < RETRIES:
            print(f"lime_ru_metrika_api: WARN {date_from} HTTP {resp.status_code}, "
                  f"попытка {attempt} из {RETRIES}, повтор через {RETRY_SLEEP * attempt}с")
            time.sleep(RETRY_SLEEP * attempt)

    resp.raise_for_status()
    return []  # недостижимо
