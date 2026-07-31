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
    orders = _empty(dates)

    # Трафик = DAU: уникальные устройства за день (не сессии). Собираем множества device_id
    # по (дата, срез, paid/organic), затем считаем len. Устройство с сессиями в обоих
    # сегментах за день попадёт в оба (Total ≥ суммы уникальных) — DAU по сегментам не аддитивен.
    scopes = (_GCC,) + tuple(ISO_CODE.values())
    tdev = {d: {s: {"app_org": set(), "app_paid": set()} for s in scopes} for d in dates}
    for r in sessions:
        iso = (r.get("session_start_datetime") or "")[:10]
        code = ISO_CODE.get(r.get("country_iso_code"))
        did = r.get("appmetrica_device_id")
        if iso not in dset or not code or not did:
            continue
        field = app_bucket(attribute(did, r["session_start_datetime"], touches))
        tdev[iso][_GCC][field].add(did)
        tdev[iso][code][field].add(did)

    traffic = _empty(dates)
    for iso in dates:
        for s in scopes:
            for f in ("app_org", "app_paid"):
                traffic[iso][s][f] = len(tdev[iso][s][f])

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


# Лёгкий набор полей установки для атрибуции: только device/время/партнёр. БЕЗ
# click_url_parameters (тяжёлый JSON ссылки трекера — для paid/organic не нужен, атрибуция
# идёт по publisher_name) — это резко уменьшает async-экспорт installations и его подготовку
# (тяжёлое окно упиралось в 20-мин таймаут Logs API).
_LEAN_INSTALL_FIELDS = "appmetrica_device_id,install_datetime,publisher_name"


def _fetch_touches(token: str, app_id: str, look_from: str, dmax: str) -> dict:
    """Касания устройств (установки + диплинки) за окно [look_from, dmax], лёгкие поля."""
    from sync.appmetrica_logs import fetch_export
    installs = fetch_export("installations", app_id, token, look_from, dmax, _LEAN_INSTALL_FIELDS)
    deeplinks = fetch_export("deeplinks", app_id, token, look_from, dmax,
                             "appmetrica_device_id,event_datetime,publisher_name")
    return build_touches(installs, deeplinks)


def fetch_app_traffic(token: str, app_id: str, dates: list[str],
                      lookback_days: int = 90) -> dict:
    """ТОЛЬКО app-трафик (DAU) GCC за `dates`: installs+deeplinks+sessions, БЕЗ purchases.

    Для добора трафика когда заказы не нужны — 3 лёгких экспорта вместо 4 тяжёлых, надёжнее
    проходит async-подготовку Logs API. Возвращает traffic (date → scope → app_org/app_paid).
    """
    from sync.appmetrica_logs import fetch_sessions

    dmin, dmax = min(dates), max(dates)
    look_from = (date.fromisoformat(dmin) - timedelta(days=lookback_days)).isoformat()
    touches = _fetch_touches(token, app_id, look_from, dmax)
    sessions = fetch_sessions(app_id, token, dmin, dmax, country=True)
    print(f"gcc_app_traffic: касаний-устройств {len(touches)}, сессий {len(sessions)} "
          f"(окно {dmin}..{dmax}, lookback {lookback_days}д)")
    traffic, _ = aggregate(sessions, [], touches, dates)
    return traffic


def fetch_app(token: str, app_id: str, dates: list[str], lookback_days: int = 90,
              event_name: str = "purchase") -> tuple[dict, dict]:
    """Собрать app-трафик и заказы GCC за `dates`. Касания тянем с lookback назад."""
    from sync.appmetrica_logs import fetch_purchase_events, fetch_sessions

    dmin, dmax = min(dates), max(dates)
    look_from = (date.fromisoformat(dmin) - timedelta(days=lookback_days)).isoformat()

    touches = _fetch_touches(token, app_id, look_from, dmax)  # лёгкие поля (без click_url_parameters)
    sessions = fetch_sessions(app_id, token, dmin, dmax, country=True)
    purchases = fetch_purchase_events(app_id, token, dmin, dmax, event_name, country=True)
    print(f"gcc_app: касаний-устройств {len(touches)}, сессий {len(sessions)}, "
          f"покупок {len(purchases)} (окно {dmin}..{dmax}, lookback {lookback_days}д)")
    return aggregate(sessions, purchases, touches, dates)
