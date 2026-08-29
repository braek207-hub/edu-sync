"""Просмотры карточек товаров Bjorn через Logs API Метрики.

Ступени «открыл карточку товара» нет в stat API вовсе — она живёт только в сырых хитах.
Берём хиты (визит, время, адрес, заголовок) и визиты (визит, источник, кампания), сшиваем
по visitID и считаем просмотры страниц /product/ в грани день × модель × источник × товар.

Три вещи, каждая замерена пробами 10, 14 и 15, и каждая ломает синк, если её не знать:

1. Создание запроса лога идёт на /logrequests, а статус и выгрузка — на /logrequest/{id}.
   Единственное и множественное число разные; на этом падала проба 9.
2. Logs API принимает наборы полей first* и lastSign*, но не automatic — модели automatic
   в логах нет вовсе (проверено /logrequests/evaluate). Значит заполняем две модели из трёх,
   а на automatic ступень остаётся неизвестной. Это честнее нуля.
3. Источник в логах приходит машинным кодом (organic, ad, internal), а в товарной витрине —
   фразой Метрики («Переходы из поисковых систем»). Без сопоставления словарей ноль строк
   сходится с витриной: замер пробы 15 — 0,0% на любой грани с источником против 99,9% по
   одному товару. Отсюда SOURCE_LABELS ниже.

ВАЖНО про грань. Из того же замера пробы 15 был сделан вывод «на грань объявления просмотры
не ложатся», и он НЕВЕРЕН: там 0,0% давал сломанный источник, а не объявление. Перемер
29.08.2026 (probe_bjorn_card_views_ad_grain.py, день 20.08, источник переведён словарём):
у 94,3% рекламных визитов есть ym:s:lastSignDirectClickBanner, и 94,7% рекламных просмотров
находят свой ключ в bjorn_product_daily на грани источник × объявление × товар. Витрина
углублена по этому замеру (миграция 20260829170000): в ключе появились campaign_id и ad_id,
а строка-итог переехала с грани источника на грань (источник, кампания, объявление).

Запросы лога снимаются в finally: неубранные копятся на квоте 10 ГБ.
"""

from __future__ import annotations

import argparse
import re
import time
from collections import defaultdict
from typing import Any

import requests

from .common import (
    SupabaseRest,
    add_date_range_args,
    env_required,
    normalize_campaign_id,
    resolve_date_range,
)

BASE = "https://api-metrika.yandex.net/management/v1/counter/{counter}"
TABLE = "bjorn_card_views_daily"
# Ключ витрины углублён до кампании и объявления (миграция 20260829170000): перемер 29.08
# показал, что грань объявления сходится с товарной витриной на 94,7%.
ON_CONFLICT = "date,attribution,source,campaign_id,ad_id,product_name"

HIT_FIELDS = "ym:pv:visitID,ym:pv:dateTime,ym:pv:URL,ym:pv:title"

# Модель витрины → префикс полей визита в Logs API. automatic сюда не входит: такого набора
# в Logs API нет, и на этой модели ступень остаётся незаполненной.
ATTRIBUTION_PREFIX = {"first": "first", "lastsign": "lastSign"}

# Машинный код источника из логов → фраза Метрики, которой источник назван в товарной витрине.
# Без этой таблицы ключи двух витрин не пересекаются вовсе (замер пробы 15).
SOURCE_LABELS = {
    "organic": "Переходы из поисковых систем",
    "ad": "Переходы по рекламе",
    "direct": "Прямые заходы",
    "internal": "Внутренние переходы",
    "referral": "Переходы по ссылкам на сайтах",
    "social": "Переходы из социальных сетей",
    "email": "Переходы с почтовых рассылок",
    "recommend": "Переходы из рекомендательных систем",
    "messenger": "Переходы из мессенджеров",
    "saved": "Переходы с сохранённых страниц",
    "undefined": "Не определено",
}

# Хвост заголовка карточки: «Куртка Осло синяя - купить в интернет-магазине …».
BUY_TAIL = re.compile(r"\s+[-–—]\s+купить", re.IGNORECASE)


def norm_product(name: str) -> str:
    """Ключ сопоставления заголовка страницы с названием товара из витрины.

    Замер пробы 9: кладёт на товар 99,3% просмотров карточек, 96 заголовков из 98.
    Промахи — страница 404 и один товар вообще без ecommerce-событий.
    """
    return re.sub(r"\s+", " ", BUY_TAIL.split(name)[0].replace("ё", "е").lower()).strip()


def visit_fields(prefix: str) -> str:
    # Кампания и объявление берутся теми же полями, из которых их выводит товарная витрина
    # (sync_products): UTMCampaign и баннер без «M-». Иначе строки двух витрин не встанут
    # в один узел дерева на дашборде, даже если id совпадают по смыслу.
    return (
        f"ym:s:visitID,ym:s:{prefix}TrafficSource,"
        f"ym:s:{prefix}UTMCampaign,ym:s:{prefix}DirectClickOrder,ym:s:{prefix}DirectClickBanner"
    )


def logs_create(counter: str, headers: dict[str, str], source: str, fields: str, d1: str, d2: str) -> int:
    resp = requests.post(
        BASE.format(counter=counter) + "/logrequests",
        params={"date1": d1, "date2": d2, "fields": fields, "source": source},
        headers=headers,
        timeout=120,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Logs API create {source}: {resp.status_code} {resp.text[:300]}")
    return resp.json()["log_request"]["request_id"]


def logs_wait(counter: str, headers: dict[str, str], request_id: int) -> dict[str, Any]:
    # Замер пробы 10: день готовится за 33 секунды; окно в четыре недели — дольше, отсюда потолок.
    for _ in range(80):
        resp = requests.get(
            BASE.format(counter=counter) + f"/logrequest/{request_id}",
            headers=headers,
            timeout=120,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Logs API status: {resp.status_code} {resp.text[:300]}")
        info = resp.json()["log_request"]
        if info["status"] == "processed":
            return info
        if info["status"] in {"processing_failed", "canceled"}:
            raise RuntimeError(f"Logs API request {request_id} закончился статусом {info['status']}")
        time.sleep(15)
    raise RuntimeError(f"Logs API request {request_id} не подготовился за 20 минут")


def logs_text(counter: str, headers: dict[str, str], request_id: int, info: dict[str, Any]) -> str:
    parts = []
    for part in info.get("parts", []):
        resp = requests.get(
            BASE.format(counter=counter) + f"/logrequest/{request_id}/part/{part['part_number']}/download",
            headers=headers,
            timeout=900,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Logs API download: {resp.status_code} {resp.text[:300]}")
        parts.append(resp.text)
    return "".join(parts)


def logs_clean(counter: str, headers: dict[str, str], request_id: int) -> None:
    requests.post(
        BASE.format(counter=counter) + f"/logrequest/{request_id}/clean",
        headers=headers,
        timeout=120,
    )


def fetch(counter: str, headers: dict[str, str], source: str, fields: str, d1: str, d2: str) -> str:
    request_id = logs_create(counter, headers, source, fields, d1, d2)
    try:
        return logs_text(counter, headers, request_id, logs_wait(counter, headers, request_id))
    finally:
        logs_clean(counter, headers, request_id)


def strip_banner(value: str) -> str:
    """id объявления из логов. Пусто, ноль и нераскрытый UTM-шаблон «{...}» — не id."""
    text = (value or "").strip()
    if text in {"", "0", "None", "--", "null", "(not set)"}:
        return ""
    if text.startswith("{") and text.endswith("}"):
        return ""
    return text[2:] if text.startswith("M-") else text


def parse_visits(text: str) -> dict[str, tuple[str, str, str]]:
    """visitID → (фраза источника, id кампании, id объявления) в терминах товарной витрины."""
    out: dict[str, tuple[str, str, str]] = {}
    for index, line in enumerate(text.splitlines()):
        if index == 0:
            continue
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        code = parts[1].strip().lower()
        source = SOURCE_LABELS.get(code, "Не определено")
        # UTMCampaign первым — им кампанию называет товарная витрина; DirectClickOrder
        # подхватывает визиты, где метка не раскрылась.
        campaign = normalize_campaign_id(parts[2]) or strip_banner(parts[3])
        out[parts[0]] = (source, campaign, strip_banner(parts[4]))
    return out


def parse_card_views(hits: str) -> list[tuple[str, str, str]]:
    """(день, visitID, ключ товара) по каждому просмотру карточки.

    Карточки живут только под /product/ — замер пробы 10: 16 137 просмотров там против
    /blog/ и /catalog/, где карточек нет вовсе.
    """
    views: list[tuple[str, str, str]] = []
    skipped: dict[str, int] = defaultdict(int)
    for index, line in enumerate(hits.splitlines()):
        if index == 0:
            continue
        parts = line.split("\t")
        if len(parts) < 4 or "/product/" not in parts[2]:
            continue
        product = norm_product(parts[3])
        if not product:
            skipped[parts[3][:70]] += 1
            continue
        views.append((parts[1][:10], parts[0], product))
    for title, count in sorted(skipped.items(), key=lambda kv: -kv[1])[:10]:
        print(f"[bjorn/card_views] заголовок без товара ({count}): {title}")
    return views


def build_rows(
    views: list[tuple[str, str, str]],
    visits_by_attribution: dict[str, dict[str, tuple[str, str, str]]],
    products: dict[str, str],
) -> list[dict[str, Any]]:
    """Свёртка просмотров в строки витрины. Товар берётся из справочника товарной витрины:
    имя должно совпасть с ней символ в символ, иначе ступень не приклеится к воронке.

    Строка с пустым product_name — итог среза (источник, кампания, объявление) за день:
    сколько РАЗНЫХ визитов открыли хоть какую-то карточку. Без неё ступень воронки
    приходится складывать по товарам, а за визит открывают несколько карточек — сумма даёт
    5 792 «визита» там, где их 2 382 (замер 29.08.2026), и конверсия вылезает за 100%.
    Сумма итогов по объявлениям внутри источника равна прежнему итогу источника: у визита
    ровно одно объявление последнего значимого перехода. Итог считается по ВСЕМ просмотрам
    /product/, включая товары без строки в товарной витрине."""
    agg: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = defaultdict(
        lambda: {"card_views": 0, "visit_ids": set()}
    )
    unknown: dict[str, int] = defaultdict(int)

    for attribution, visit_source in visits_by_attribution.items():
        for day, visit_id, product_key in views:
            source, campaign, ad = visit_source.get(visit_id, ("Не определено", "", ""))
            total = agg[(day, attribution, source, campaign, ad, "")]
            total["card_views"] += 1
            total["visit_ids"].add(visit_id)

            product_name = products.get(product_key)
            if product_name is None:
                unknown[product_key] += 1
                continue
            bucket = agg[(day, attribution, source, campaign, ad, product_name)]
            bucket["card_views"] += 1
            bucket["visit_ids"].add(visit_id)

    # Товары без строки в товарной витрине — молча терять нельзя: это либо новый товар,
    # у которого ещё нет ecommerce-событий, либо разошедшийся заголовок.
    for product, count in sorted(unknown.items(), key=lambda kv: -kv[1])[:10]:
        print(f"[bjorn/card_views] товара нет в витрине ({count} просмотров): {product[:60]}")

    return [
        {
            "date": key[0],
            "attribution": key[1],
            "source": key[2],
            "campaign_id": key[3],
            "ad_id": key[4],
            "product_name": key[5],
            "card_views": value["card_views"],
            "card_viewers": len(value["visit_ids"]),
        }
        for key, value in sorted(agg.items())
    ]


def load_products(supabase: SupabaseRest, date_from: str, date_to: str) -> dict[str, str]:
    rows = supabase.select_date_range(
        "bjorn_product_daily", date_from, date_to, select="product_name"
    )
    return {norm_product(r["product_name"]): r["product_name"] for r in rows}


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Bjorn product card views to Supabase.")
    add_date_range_args(parser)
    args = parser.parse_args()
    date_from, date_to = resolve_date_range(args)

    counter = env_required("METRICA_COUNTER_ID")
    headers = {"Authorization": f"OAuth {env_required('METRICA_TOKEN')}"}

    hits = fetch(counter, headers, "hits", HIT_FIELDS, date_from, date_to)
    views = parse_card_views(hits)
    print(f"[bjorn/card_views] просмотров карточек: {len(views)}")

    visits_by_attribution = {
        attribution: parse_visits(
            fetch(counter, headers, "visits", visit_fields(prefix), date_from, date_to)
        )
        for attribution, prefix in ATTRIBUTION_PREFIX.items()
    }

    supabase = SupabaseRest()
    products = load_products(supabase, date_from, date_to)
    print(f"[bjorn/card_views] товаров в справочнике витрины: {len(products)}")

    rows = build_rows(views, visits_by_attribution, products)
    supabase.delete_date_range(TABLE, date_from, date_to)
    supabase.upsert(TABLE, rows, ON_CONFLICT)
    print(f"[bjorn/card_views] {len(rows)} строк за {date_from}..{date_to}")


if __name__ == "__main__":
    main()
