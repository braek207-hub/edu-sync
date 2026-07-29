# -*- coding: utf-8 -*-
"""Shopify Admin API (GCC-магазин lime-shop-prod): страна ДОСТАВКИ заказа.

TW-эндпоинт get-orders-with-journeys не отдаёт страну — только домен витрины из
journey. Но люди заходят на витрину одной страны, а доставка в другую (пример:
заказ на ae.limestore.com, доставка Doha/Qatar). Правда о стране — в Shopify
(shipping address). Джойним по order_id: страна из Shopify, источник из TW.

Тянем через GraphQL Admin API постранично. Отдаём {order_id(str): страна(RU)}.

Авторизация: grant `client_credentials` (client_id+client_secret → короткоживущий
Admin API access token shpat_, ~24ч). Токен добываем каждый прогон — не храним.
API automation token (atkn_) НЕ подходит — он только для Shopify CLI (deploy).

ENV: API_LIME_SHOPIFY (CLIENT SECRET приложения, shpss_…),
     GCC_SHOPIFY_CLIENT_ID (default 0c45cd1d…, публичный),
     GCC_SHOPIFY_SHOP (default lime-shop-prod.myshopify.com),
     GCC_SHOPIFY_API_VERSION (default 2025-07).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

SHOP = os.environ.get("GCC_SHOPIFY_SHOP") or "lime-shop-prod.myshopify.com"
CLIENT_ID = os.environ.get("GCC_SHOPIFY_CLIENT_ID") or "0c45cd1d60b22691368f2cb8ab38fe11"
API_VERSION = os.environ.get("GCC_SHOPIFY_API_VERSION") or "2025-07"


def get_access_token(client_secret: str) -> str:
    """client_credentials → Admin API access token (shpat_, ~24ч). Не хранится."""
    url = f"https://{SHOP}/admin/oauth/access_token"
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": client_secret,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Shopify token HTTP {e.code}: {e.read().decode('utf-8')[:300]}")
    tok = data.get("access_token")
    if not tok:
        raise RuntimeError(f"Shopify token: нет access_token в ответе ({str(data)[:200]})")
    return tok

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

_QUERY = """
query($q: String!, $after: String) {
  orders(first: 250, query: $q, after: $after, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    edges { node { id shippingAddress { countryCodeV2 } } }
  }
}
"""

_RETRIES = int(os.environ.get("GCC_SHOPIFY_RETRIES") or "4")
_BACKOFF = int(os.environ.get("GCC_SHOPIFY_BACKOFF") or "3")


def _post(token: str, variables: dict) -> dict:
    """POST GraphQL с ретраем 429/5xx (throttling Shopify) и разбором ошибок."""
    url = f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"
    body = json.dumps({"query": _QUERY, "variables": variables}).encode("utf-8")
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


def _order_id(gid: str) -> str:
    """gid://shopify/Order/7091092619586 → '7091092619586' (как order_id у TW)."""
    return (gid or "").rsplit("/", 1)[-1]


def fetch_order_countries(client_secret: str, date_from: str, date_to: str) -> dict[str, str | None]:
    """{order_id(str): страна доставки(RU) | None} за [date_from; date_to] по дате создания.

    None — доставка вне Залива или без адреса (заказ уйдёт в GCC-тотал, не в страну).
    `to` инклюзивен: используем created_at:<=date_to. Access token добываем сами.
    """
    token = get_access_token(client_secret)
    q = f"created_at:>={date_from} created_at:<={date_to}"
    out: dict[str, str | None] = {}
    after = None
    while True:
        data = _post(token, {"q": q, "after": after})
        conn = data["orders"]
        for edge in conn["edges"]:
            node = edge["node"]
            addr = node.get("shippingAddress") or {}
            code = addr.get("countryCodeV2")
            out[_order_id(node["id"])] = COUNTRY_BY_CODE.get(code)
        if conn["pageInfo"]["hasNextPage"]:
            after = conn["pageInfo"]["endCursor"]
        else:
            break
    return out
