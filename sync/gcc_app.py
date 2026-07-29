# -*- coding: utf-8 -*-
"""AppMetrica GCC (app 6299245): app-трафик (sessions) и заказы (e-commerce) по
стране × paid/organic через last-touch атрибуцию.

Источник живёт на КАСАНИИ (установка = привлечение, диплинк = ре-энгейджмент), не на
сессии/покупке. Для каждой сессии/покупки берём последнее касание устройства с временем
≤ времени события (last-touch): диплинк если есть, иначе установка. Партнёр касания →
paid/organic. Lookback = окно атрибуции (старше не тянем — как и положено атрибуции).

Фильтр по стране события (country_iso_code ∈ 5 стран Залива). Срез «GCC» = сумма пяти
(не как у web, где GCC включает country=NULL).
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta

# ISO страны события → код колонки книги. Только Залив; прочие страны (RU/KZ/…) отброшены.
ISO_CODE = {"AE": "UAE", "SA": "KSA", "QA": "QA", "KW": "KW", "OM": "OM"}
_GCC = "GCC"

# Партнёр платного привлечения/ремаркетинга. Матчим по подстроке (Yandex.Direct и
# Yandex.Direct Auto-Tracking, VK Ads / vkads и т.п.). Всё остальное — органика.
_PAID_KW = ("google ads", "yandex.direct", "vk ads", "vkads", "facebook", "instagram",
            "meta ads", "mytarget", "tiktok", "snapchat", "bing", "advertisement engine")


def app_bucket(publisher: str | None) -> str:
    """'app_paid' если партнёр — платная сеть, иначе 'app_org' (в т.ч. пусто/unknown)."""
    p = (publisher or "").lower()
    return "app_paid" if any(k in p for k in _PAID_KW) else "app_org"


def build_touches(installs: list[dict], deeplinks: list[dict]) -> dict[str, list[tuple[str, str]]]:
    """device_id → отсортированный список касаний (timestamp, publisher).

    Установка: install_datetime; диплинк: event_datetime. Формат времени фиксированный
    'YYYY-MM-DD HH:MM:SS' → лексикографическая сортировка = хронологическая.
    """
    touches: dict[str, list[tuple[str, str]]] = {}
    for r in installs:
        did = r.get("appmetrica_device_id")
        if did:
            touches.setdefault(did, []).append((r.get("install_datetime") or "",
                                                r.get("publisher_name") or ""))
    for r in deeplinks:
        did = r.get("appmetrica_device_id")
        if did:
            touches.setdefault(did, []).append((r.get("event_datetime") or "",
                                                r.get("publisher_name") or ""))
    for v in touches.values():
        v.sort()
    return touches


def attribute(device: str, ev_ts: str, touches: dict) -> str:
    """Партнёр последнего касания устройства с временем ≤ ev_ts. '' если касаний нет."""
    lst = touches.get(device)
    if not lst:
        return ""
    pub = ""
    for ts, p in lst:
        if ts <= ev_ts:
            pub = p
        else:
            break
    return pub


def _empty(dates: list[str]) -> dict:
    scopes = (_GCC,) + tuple(ISO_CODE.values())
    return {d: {s: {"app_org": 0, "app_paid": 0} for s in scopes} for d in dates}


def aggregate(sessions: list[dict], purchases: list[dict], touches: dict,
              dates: list[str]) -> tuple[dict, dict]:
    """(traffic, orders): date → scope → {app_org, app_paid}.

    traffic = число сессий; orders = число покупок (дедуп по transaction_id). Оба
    фильтруются по стране Залива и атрибутируются last-touch.
    """
    dset = set(dates)
    traffic = _empty(dates)
    orders = _empty(dates)

    for r in sessions:
        iso = (r.get("session_start_datetime") or "")[:10]
        code = ISO_CODE.get(r.get("country_iso_code"))
        if iso not in dset or not code:
            continue
        field = app_bucket(attribute(r.get("appmetrica_device_id"), r["session_start_datetime"], touches))
        traffic[iso][_GCC][field] += 1
        traffic[iso][code][field] += 1

    seen: set[str] = set()
    for r in purchases:
        iso = (r.get("event_datetime") or "")[:10]
        code = ISO_CODE.get(r.get("country_iso_code"))
        if iso not in dset or not code:
            continue
        try:
            txn = json.loads(r.get("event_json") or "{}").get("transaction_id")
        except Exception:  # noqa: BLE001
            txn = None
        if txn and txn in seen:
            continue
        if txn:
            seen.add(txn)
        field = app_bucket(attribute(r.get("appmetrica_device_id"), r["event_datetime"], touches))
        orders[iso][_GCC][field] += 1
        orders[iso][code][field] += 1

    return traffic, orders


def fetch_app(token: str, app_id: str, dates: list[str], lookback_days: int = 90,
              event_name: str = "purchase") -> tuple[dict, dict]:
    """Собрать app-трафик и заказы GCC за `dates`. Касания тянем с lookback назад."""
    from sync.appmetrica_logs import (
        fetch_export, fetch_installations, fetch_purchase_events, fetch_sessions,
    )

    dmin, dmax = min(dates), max(dates)
    look_from = (date.fromisoformat(dmin) - timedelta(days=lookback_days)).isoformat()

    installs = fetch_installations(app_id, token, look_from, dmax)
    deeplinks = fetch_export("deeplinks", app_id, token, look_from, dmax,
                             "appmetrica_device_id,event_datetime,publisher_name")
    touches = build_touches(installs, deeplinks)

    sessions = fetch_sessions(app_id, token, dmin, dmax, country=True)
    purchases = fetch_purchase_events(app_id, token, dmin, dmax, event_name, country=True)
    print(f"gcc_app: касаний-устройств {len(touches)}, сессий {len(sessions)}, "
          f"покупок {len(purchases)} (окно {dmin}..{dmax}, lookback {lookback_days}д)")
    return aggregate(sessions, purchases, touches, dates)
