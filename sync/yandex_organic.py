# -*- coding: utf-8 -*-
"""Яндекс.Метрика Stat API → lime_yandex_organic (органика Яндекса по гео: KZ, Залив).

SEO-половина Яндекс-вкладок блока «Брендовый спрос»: рядом со спросом Wordstat по тому
же гео показываем, сколько визитов пришло из органической выдачи Яндекса в этом гео.

ПОЧЕМУ МЕТРИКА, А НЕ ВЕБМАСТЕР (как в RU). У API Вебмастера гео-среза нет: параметры
метода search-queries — order_by / query_indicator / device_type_indicator / date_from /
date_to / offset / limit, и посланные region_ids|region_id|country он молча игнорирует
(живой зонд 2026-08-26: все варианты вернули один и тот же ряд бит-в-бит). Хостов в
аккаунте два (limestore.com, lime-shop.com), казахстанские заходы сидят внутри общего
лимитстора — отделить их хостом нельзя. Визит из выдачи = клик по выдаче, поэтому ряд
однороден с TOTAL_CLICKS Вебмастера в RU.

БРЕНД НЕ ВЫЧИТАЕМ, как и в RU-Вебмастере: замер 2026-08-26 по KZ за 30 дней — фраза
видна лишь у 961 визита из 6519 (15%, остальное Яндекс прячет), среди видимых 97%
брендовые, а «небренд» — почти сплошь опечатки бренда («лаим», «limr», «liime»).
Фильтр по фразе выбросил бы 85% ряда ради 3% примеси.

Env: LIME_METRIKA_TOKEN, LIME_METRIKA_COUNTER_ID (default 23504302), DATABASE_URL.
Запуск: python -m sync.yandex_organic  (или из sync_brand.py).
"""
import datetime as dt
import os
import time

import requests

API_URL = "https://api-metrika.yandex.net/stat/v1/data"
COUNTER_ID = os.environ.get("LIME_METRIKA_COUNTER_ID") or "23504302"

RETRIES = int(os.environ.get("LIME_ORGANIC_RETRIES") or "3")
RETRY_SLEEP = int(os.environ.get("LIME_ORGANIC_RETRY_SLEEP") or "5")
ROW_LIMIT = 10000

# Поисковые системы Яндекса, которые считаем органикой выдачи. Смарт-камера, Яндекс.Картинки
# и «поиск по тегам» НЕ входят: это не переходы по текстовой выдаче (в KZ вместе ~1,5%).
YANDEX_ENGINES = ("yandex_search", "yandex_mobile")

# Регионы вкладок: ключ ряда → страны визита (ym:s:regionCountryName, английские названия
# API). У KZ страна одна и в таблицу пишется пустой (регион = страна, как у lime_gsc_seo),
# у Залива — своя строка на страну, чтобы работал селектор стран в виджете.
REGIONS: dict[str, dict[str, str]] = {
    # {страна API: страна в витрине}
    "kz": {"Kazakhstan": ""},
    "gcc": {
        "United Arab Emirates": "ОАЭ",
        "Saudi Arabia": "Саудовская Аравия",
        "Kuwait": "Кувейт",
        "Qatar": "Катар",
        "Bahrain": "Бахрейн",
        "Oman": "Оман",
    },
}


def engines_filter() -> str:
    return "(" + " OR ".join(f"ym:s:lastsignSearchEngine=='{e}'" for e in YANDEX_ENGINES) + ")"


def countries_filter(countries: list[str]) -> str:
    return "(" + " OR ".join(f"ym:s:regionCountryName=='{c}'" for c in countries) + ")"


def parse_rows(resp: dict, country_map: dict[str, str]) -> list[dict]:
    """Ответ Stat API (dims = date, regionCountryName) → [{day, country, visits}].

    Позиции измерений читаются из эха запроса (resp["query"]["dimensions"]), поэтому
    перестановка измерения не ломает разбор. Страна маппится в имя витрины; незнакомая
    страна пропускается (фильтр запроса её и не должен был вернуть).
    """
    queried = (resp.get("query") or {}).get("dimensions") or []
    pos = {name: i for i, name in enumerate(queried)}
    out: list[dict] = []
    for row in resp.get("data", []):
        dims = row.get("dimensions") or []
        day = (dims[pos["ym:s:date"]] or {}).get("name")
        api_country = (dims[pos["ym:s:regionCountryName"]] or {}).get("name")
        if not day or api_country not in country_map:
            continue
        out.append({
            "day": day,
            "country": country_map[api_country],
            "visits": int(round(float((row.get("metrics") or [0])[0] or 0))),
        })
    return out


def fetch_organic(region: str, date_from: str, date_to: str, token: str) -> list[dict]:
    """Дневная органика Яндекса региона → [{day, country, visits}] (с пагинацией)."""
    country_map = REGIONS[region]
    params = {
        "ids": COUNTER_ID,
        "date1": date_from,
        "date2": date_to,
        "metrics": "ym:s:visits",
        "dimensions": "ym:s:date,ym:s:regionCountryName",
        "filters": f"{engines_filter()} AND {countries_filter(list(country_map))}",
        "accuracy": "full",
        "limit": ROW_LIMIT,
    }
    headers = {"Authorization": f"OAuth {token}"}
    rows: list[dict] = []
    offset = 1
    while True:
        resp = None
        for attempt in range(RETRIES):
            r = requests.get(API_URL, params={**params, "offset": offset}, headers=headers, timeout=120)
            if r.status_code == 200:
                resp = r.json()
                break
            # 429/5xx у Stat API транзиентны (как в lime_kz_metrika_api): ждём и повторяем.
            if r.status_code in (429, 500, 502, 503, 504) and attempt < RETRIES - 1:
                time.sleep(RETRY_SLEEP * (attempt + 1))
                continue
            r.raise_for_status()
        if resp is None:
            break
        batch = parse_rows(resp, country_map)
        rows += batch
        if len(resp.get("data", [])) < ROW_LIMIT:
            break
        offset += ROW_LIMIT
    return rows


def sync_yandex_organic(date_from: str, date_to: str, region: str) -> int:
    """Синк дневной органики региона. Возвращает число записанных строк день×страна."""
    token = os.environ["LIME_METRIKA_TOKEN"]
    rows = fetch_organic(region, date_from, date_to, token)
    if not rows:
        return 0
    from sync.db import get_connection  # ленивый импорт psycopg2

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO lime_yandex_organic (day, region, country, visits, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (day, region, country)
                DO UPDATE SET visits = EXCLUDED.visits, updated_at = now()
                """,
                [(r["day"], region, r["country"], r["visits"]) for r in rows],
            )
        conn.commit()
    return len(rows)


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    frm = os.environ.get("ORGANIC_FROM") or (dt.date.today() - dt.timedelta(days=90)).isoformat()
    to = dt.date.today().isoformat()
    for reg in REGIONS:
        print(f"yandex-organic[{reg}]: {sync_yandex_organic(frm, to, reg)} строк (с {frm})")
