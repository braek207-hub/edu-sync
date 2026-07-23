# -*- coding: utf-8 -*-
"""Яндекс.Метрика Stat API — RU-срез счётчика LIME (общий с KZ, 23504302).

RU и KZ живут на одном счётчике и домене limestore.com, разделяем гео-страной визита
(как в sync/lime_kz_metrika_api.py — там фильтр Kazakhstan). Здесь фильтр Russia.

Назначение — обогатить основную RU-таблицу (витрина PROCONTEXT) поведением и post-click
воронкой Метрики ПО КАМПАНИЯМ: визиты/отказы/глубина + корзина/оформление/покупки/выручка
в разрезе UTM-кампании и канала. Post-view остаётся за Медиаметрикой (отдельный источник).

Разрез (DIMENSIONS), метрики (METRICS/METRIC_FIELDS), цели и парсер — общие с KZ-модулем,
импортируем их, чтобы не расходились: единственное отличие RU от KZ — гео-фильтр.
"""
import os
import time

import requests

from sync.lime_kz_metrika_api import (
    API_URL,
    DIMENSIONS,
    METRICS,
    parse_metrika_kz,
)

RETRIES = int(os.environ.get("LIME_RU_METRIKA_RETRIES") or "3")
RETRY_SLEEP = int(os.environ.get("LIME_RU_METRIKA_RETRY_SLEEP") or "5")

# RU-срез того же счётчика: страна визита — Россия (KZ-модуль фильтрует Kazakhstan).
GEO_FILTER = "ym:s:regionCountryName=='Russia'"


def fetch_ru_traffic(counter_id, token: str, date_from: str, date_to: str) -> list[dict]:
    """Забрать RU-срез (гео Россия) за период. Разбор — общий parse_metrika_kz.

    Повторяет запрос при транзиентной ошибке Stat API (как KZ-модуль): API периодически
    отдаёт 400 на отдельной дате, тот же запрос минутой позже — 200; без повтора одна
    осечка роняет весь прогон.

    Args:
        counter_id: id счётчика (23504302).
        token: OAuth-токен Яндекса с доступом к счётчику.
        date_from, date_to: даты YYYY-MM-DD включительно.

    Returns:
        Строки parse_metrika_kz (измерения + метрики METRIC_FIELDS).

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
            print(f"lime_ru_metrika_api: WARN {date_from} HTTP {resp.status_code}, "
                  f"попытка {attempt} из {RETRIES}, повтор через {RETRY_SLEEP * attempt}с")
            time.sleep(RETRY_SLEEP * attempt)

    resp.raise_for_status()
    return []  # недостижимо: raise_for_status уже бросил на не-200
