# -*- coding: utf-8 -*-
"""Shopify Admin API (GCC-магазин lime-shop-prod): страна ДОСТАВКИ заказа.

TW-эндпоинт get-orders-with-journeys не отдаёт страну — только домен витрины из
journey. Но люди заходят на витрину одной страны, а доставка в другую (пример:
заказ на ae.limestore.com, доставка Doha/Qatar). Правда о стране — в Shopify
(shipping address). Джойним по order_id: страна из Shopify, источник из TW.

Тянем через GraphQL Admin API постранично. Отдаём {order_id(str): страна(RU)}.

Авторизация: offline Admin API access token (shpca_/shpat_), полученный OAuth
authorization-code-grant один раз (client_credentials на этом магазине запрещён:
shop_not_permitted; atkn_ automation token — только для Shopify CLI). Токен
бессрочный, лежит в секрете API_LIME_SHOPIFY, шлём в X-Shopify-Access-Token.

ENV: API_LIME_SHOPIFY (offline Admin API access token shpca_/shpat_),
     GCC_SHOPIFY_SHOP (default lime-shop-prod.myshopify.com),
     GCC_SHOPIFY_API_VERSION (default 2025-07).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

SHOP = os.environ.get("GCC_SHOPIFY_SHOP") or "lime-shop-prod.myshopify.com"
API_VERSION = os.environ.get("GCC_SHOPIFY_API_VERSION") or "2025-07"

# ISO alpha-2 страны доставки → русское имя как в lime_stats (region='gcc').
# Только Залив; прочие страны доставки → None (уедут в GCC-тотал, не в колонку страны).
COUNTRY_BY_CODE = {
    "AE": "ОАЭ",
    "SA": "Саудовская Аравия",
    "QA": "Катар",
    "KW": "Кувейт",
    "OM": "Оман",
    "BH": "Бахрейн",
}

# Канал заказа: app.name == 'Online Store' — веб-витрина; 'Lime Mobile BFF' — приложение.
# App-заказы чекаутятся через тот же Shopify → без исключения считались бы и в web(TW),
# и в app(AppMetrica). Веб = только Online Store.
WEB_CHANNEL = "Online Store"

_QUERY = """
query($q: String!, $after: String) {
  orders(first: 250, query: $q, after: $after, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    edges { node { id app { name } shippingAddress { countryCodeV2 } } }
  }
}
"""

_RETRIES = int(os.environ.get("GCC_SHOPIFY_RETRIES") or "4")
_BACKOFF = int(os.environ.get("GCC_SHOPIFY_BACKOFF") or "3")


def _post(token: str, variables: dict, query: str | None = None) -> dict:
    """POST GraphQL с ретраем 429/5xx (throttling Shopify) и разбором ошибок."""
    url = f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"
    body = json.dumps({"query": query or _QUERY, "variables": variables}).encode("utf-8")
    for attempt in range(1, _RETRIES + 1):
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < _RETRIES:
                time.sleep(_BACKOFF * attempt)
                continue
            raise RuntimeError(f"Shopify HTTP {e.code}: {e.read().decode('utf-8')[:300]}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < _RETRIES:
                time.sleep(_BACKOFF * attempt)
                continue
            raise RuntimeError(f"Shopify сеть: {e}")
        # GraphQL throttling приходит с HTTP 200 и errors[].extensions.code=THROTTLED.
        errs = data.get("errors")
        if errs:
            throttled = any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errs)
            if throttled and attempt < _RETRIES:
                time.sleep(_BACKOFF * attempt)
                continue
            raise RuntimeError(f"Shopify GraphQL errors: {str(errs)[:300]}")
        return data["data"]
    raise RuntimeError("Shopify: исчерпаны попытки")


_SOURCE_QUERY = """
query($q: String!, $after: String) {
  orders(first: 100, query: $q, after: $after, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    edges { node {
      id name createdAt sourceName
      app { name }
      channelInformation { channelDefinition { channelName handle } }
      shippingAddress { countryCodeV2 }
    } }
  }
}
"""


def fetch_order_sources(token: str, date_from: str, date_to: str) -> list[dict]:
    """Диагностика: [{order_id, name, created, source, app, channel, country}]."""
    q = f"created_at:>={date_from} created_at:<={date_to}"
    out, after = [], None
    while True:
        data = _post(token, {"q": q, "after": after}, query=_SOURCE_QUERY)
        conn = data["orders"]
        for e in conn["edges"]:
            n = e["node"]
            ch = (((n.get("channelInformation") or {}).get("channelDefinition")) or {})
            out.append({
                "order_id": _order_id(n["id"]),
                "name": n.get("name"),
                "created": (n.get("createdAt") or "")[:10],
                "source": n.get("sourceName"),
                "app": (n.get("app") or {}).get("name"),
                "channel": ch.get("channelName") or ch.get("handle"),
                "country": ((n.get("shippingAddress") or {}).get("countryCodeV2")),
            })
        if conn["pageInfo"]["hasNextPage"]:
            after = conn["pageInfo"]["endCursor"]
        else:
            break
    return out


def _order_id(gid: str) -> str:
    """gid://shopify/Order/7091092619586 → '7091092619586' (как order_id у TW)."""
    return (gid or "").rsplit("/", 1)[-1]


def fetch_order_meta(token: str, date_from: str, date_to: str) -> tuple[dict[str, str | None], set[str]]:
    """(country_by_order, app_order_ids) за [date_from; date_to] по дате создания.

    country_by_order: {order_id: страна доставки(RU)|None} — None = вне Залива/без адреса.
    app_order_ids: order_id заказов НЕ из веб-витрины (app 'Lime Mobile BFF' и пр.) —
    их исключаем из web-счёта, чтобы не задвоить с AppMetrica. `to` инклюзивен.
    """
    q = f"created_at:>={date_from} created_at:<={date_to}"
    country: dict[str, str | None] = {}
    app_ids: set[str] = set()
    after = None
    while True:
        data = _post(token, {"q": q, "after": after})
        conn = data["orders"]
        for edge in conn["edges"]:
            node = edge["node"]
            oid = _order_id(node["id"])
            code = (node.get("shippingAddress") or {}).get("countryCodeV2")
            country[oid] = COUNTRY_BY_CODE.get(code)
            if ((node.get("app") or {}).get("name")) != WEB_CHANNEL:
                app_ids.add(oid)
        if conn["pageInfo"]["hasNextPage"]:
            after = conn["pageInfo"]["endCursor"]
        else:
            break
    return country, app_ids


def fetch_order_countries(token: str, date_from: str, date_to: str) -> dict[str, str | None]:
    """Только страны (обёртка над fetch_order_meta) — для пробы/обратной совместимости."""
    return fetch_order_meta(token, date_from, date_to)[0]
