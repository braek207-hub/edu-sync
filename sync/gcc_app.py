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
import urllib.parse
import urllib.request
from datetime import date, timedelta

# ISO страны события → код колонки книги. Только Залив; прочие страны (RU/KZ/…) отброшены.
ISO_CODE = {"AE": "UAE", "SA": "KSA", "QA": "QA", "KW": "KW", "OM": "OM"}
_GCC = "GCC"

# Reporting API (агрегат, синхронно). Имя страны (рус.) → код колонки.
_STAT_URL = "https://api.appmetrica.yandex.ru/stat/v1/data"
_REP_COUNTRY = {"Объединённые Арабские Эмираты": "UAE", "Саудовская Аравия": "KSA",
                "Катар": "QA", "Кувейт": "KW", "Оман": "OM"}


def fetch_app_dau_total(token: str, app_id: str, dates: list[str]) -> dict:
    """DAU total по стране из Reporting API (`ym:s:devices`) — синхронно, надёжно, полный день.

    Возвращает {date: {scope: total}}, scope ∈ {GCC, UAE, KSA, QA, KW, OM}; GCC = сумма 5 стран
    Залива (device в двух странах за день считается в обеих — минорный двойной счёт, как per-country).
    Reporting = «Аудитория» в UI, поэтому total точный; сплит paid/organic Reporting не даёт
    (только Logs last-touch). Заказы здесь не считаются.
    """
    params = {"id": app_id, "date1": min(dates), "date2": max(dates), "accuracy": "1",
              "limit": "10000", "metrics": "ym:s:devices",
              "dimensions": "ym:s:date,ym:s:regionCountry"}
    url = f"{_STAT_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"OAuth {token}"})
    with urllib.request.urlopen(req, timeout=90) as r:
        data = json.loads(r.read().decode("utf-8"))

    dset = set(dates)
    out = {d: {s: 0 for s in (_GCC,) + tuple(ISO_CODE.values())} for d in dates}
    for row in data.get("data", []):
        dims = [d.get("name") for d in row.get("dimensions", [])]
        if len(dims) < 2:
            continue
        iso, code = dims[0], _REP_COUNTRY.get(dims[1])
        if iso not in dset or not code:
            continue
        dev = int(round(row["metrics"][0]))
        out[iso][code] += dev
        out[iso][_GCC] += dev
    return out

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
                      lookback_days: int = 90, chunk_days: int | None = None) -> dict:
    """ТОЛЬКО app-трафик (DAU) GCC за `dates`: installs+deeplinks+sessions, БЕЗ purchases.

    Сессии тянем ЧАНКАМИ по chunk_days: приложение мультистрановое (RU/KZ/…+Залив), единый
    экспорт сессий за большое окно не готовился за отведённое время Logs API. Мелкие окна
    готовятся быстро; DAU считается по-дневно (дни не пересекают чанки → мержим без дедупа).
    Касания (installs+deeplinks, лёгкие поля) тянем один раз на всё окно. Заказы не считаем.
    """
    chunk_days = chunk_days or int(os.environ.get("LIME_GCC_APP_CHUNK_DAYS") or "7")
    dmin, dmax = min(dates), max(dates)
    look_from = (date.fromisoformat(dmin) - timedelta(days=lookback_days)).isoformat()
    # Пол окна касаний: не сканировать installs раньше запуска приложения — пустой диапазон
    # раздувает и замедляет async-экспорт AppMetrica (installations упирался в таймаут).
    floor = os.environ.get("LIME_GCC_APP_LOOK_FLOOR") or "2026-06-01"  # app запущен ~2 июня'26
    if look_from < floor:
        look_from = floor
    touches = _fetch_touches(token, app_id, look_from, dmax)

    from sync.appmetrica_logs import fetch_sessions
    combined = _empty(dates)
    dmin_d, dmax_d = date.fromisoformat(dmin), date.fromisoformat(dmax)
    d, total_s = dmin_d, 0
    while d <= dmax_d:
        c_to = min(d + timedelta(days=chunk_days - 1), dmax_d)
        cdates = [(d + timedelta(days=k)).isoformat() for k in range((c_to - d).days + 1)]
        try:
            s = fetch_sessions(app_id, token, d.isoformat(), c_to.isoformat(), country=True)
        except Exception as e:  # noqa: BLE001 — сбойный чанк не роняет весь сбор (дни останутся 0)
            print(f"gcc_app_traffic чанк {d}..{c_to}: ПРОПУЩЕН ({type(e).__name__}: {e})")
            d = c_to + timedelta(days=1)
            continue
        total_s += len(s)
        t, _ = aggregate(s, [], touches, cdates)
        for iso in cdates:
            combined[iso] = t[iso]
        print(f"gcc_app_traffic чанк {d}..{c_to}: сессий {len(s)}")
        d = c_to + timedelta(days=1)
    print(f"gcc_app_traffic: касаний-устройств {len(touches)}, сессий всего {total_s} "
          f"(окно {dmin}..{dmax}, lookback {lookback_days}д, чанк {chunk_days}д)")
    return combined


# ISO страны → русское имя (как пишет lime_stats web-пайплайн GCC).
ISO_RU = {"AE": "ОАЭ", "SA": "Саудовская Аравия", "QA": "Катар", "KW": "Кувейт", "OM": "Оман"}
_CODE_RU = {"UAE": "ОАЭ", "KSA": "Саудовская Аравия", "QA": "Катар", "KW": "Кувейт", "OM": "Оман"}


def aggregate_traffic_channels(sessions: list[dict], touches: dict,
                               dates: list[str]) -> dict:
    """DAU и сессии app по (дата, страна, канал) — publisher last-touch → таксономия.

    Returns:
        {date: {country_ru: {(channel, subchannel, traffic_type): [dau, sessions]}}}.
        DAU = уникальные устройства в срезе за день (по сегментам не аддитивен).
    """
    from sync.gcc_channels import map_app_publisher

    dset = set(dates)
    dev: dict = {d: {} for d in dates}
    ses: dict = {d: {} for d in dates}
    for r in sessions:
        iso = (r.get("session_start_datetime") or "")[:10]
        country = ISO_RU.get(r.get("country_iso_code"))
        did = r.get("appmetrica_device_id")
        if iso not in dset or not country or not did:
            continue
        key = map_app_publisher(attribute(did, r["session_start_datetime"], touches))
        dev[iso].setdefault(country, {}).setdefault(key, set()).add(did)
        ses[iso].setdefault(country, {}).setdefault(key, 0)
        ses[iso][country][key] += 1

    out: dict = {d: {} for d in dates}
    for iso in dates:
        for country, by_key in dev[iso].items():
            out[iso][country] = {k: [len(s), ses[iso][country].get(k, 0)]
                                 for k, s in by_key.items()}
    return out


def build_app_traffic_rows(total_by_day: dict, channels_by_day: dict,
                           dates: list[str]) -> list[dict]:
    """Строки app-трафика для lime_stats: тотал из Reporting (=UI), платные каналы из Logs.

    Тотал страны = Reporting (`fetch_app_dau_total`, «Аудитория» в UI, как ручной файл).
    Платные/реферальные каналы — сырые DAU из Logs (недоохват Logs остаётся в остатке).
    Остаток (тотал − размеченные каналы) кладётся в Direct — «открыл приложение сам» плюс
    неразмеченное. Нет Logs по стране → весь тотал в Direct.

    Args:
        total_by_day: {date: {code(UAE/…): total}} из fetch_app_dau_total.
        channels_by_day: {date: {country_ru: {(ch, sub, tt): [dau, sessions]}}}
            из aggregate_traffic_channels; допускается пустой ({}).

    Returns:
        [{date, country, channel, subchannel, traffic_type, users, sessions}] — Direct-строка
        одна на страну (в неё же вошёл остаток), users ≥ 0.
    """
    rows: list[dict] = []
    for iso in dates:
        for code, total in (total_by_day.get(iso) or {}).items():
            country = _CODE_RU.get(code)
            if not country or not total:
                continue
            by_key = (channels_by_day.get(iso) or {}).get(country) or {}
            direct_key = ("Direct", "Direct", "Бесплатный")
            tagged = sum(dau for k, (dau, _s) in by_key.items() if k != direct_key)
            residual = max(total - tagged, 0)
            emitted_direct = False
            for (ch, sub, tt), (dau, n_ses) in sorted(by_key.items()):
                users = residual if (ch, sub, tt) == direct_key else dau
                emitted_direct = emitted_direct or (ch, sub, tt) == direct_key
                rows.append({"date": iso, "country": country, "channel": ch,
                             "subchannel": sub, "traffic_type": tt,
                             "users": users, "sessions": n_ses})
            if not emitted_direct and residual:
                rows.append({"date": iso, "country": country, "channel": "Direct",
                             "subchannel": "Direct", "traffic_type": "Бесплатный",
                             "users": residual, "sessions": 0})
    return rows


def fetch_app_traffic_dashboard(token: str, app_id: str, dates: list[str],
                                lookback_days: int = 90) -> list[dict]:
    """App-трафик GCC для lime_stats: Reporting-тотал + best-effort канальный сплит из Logs.

    Logs упал/таймаут → строки только из Reporting (весь тотал в Direct), синк жив.
    """
    total = fetch_app_dau_total(token, app_id, dates)
    channels: dict = {}
    try:
        from sync.appmetrica_logs import fetch_sessions
        chunk_days = int(os.environ.get("LIME_GCC_APP_CHUNK_DAYS") or "7")
        dmin, dmax = min(dates), max(dates)
        look_from = (date.fromisoformat(dmin) - timedelta(days=lookback_days)).isoformat()
        floor = os.environ.get("LIME_GCC_APP_LOOK_FLOOR") or "2026-06-01"
        look_from = max(look_from, floor)
        touches = _fetch_touches(token, app_id, look_from, dmax)
        d, dmax_d = date.fromisoformat(dmin), date.fromisoformat(dmax)
        while d <= dmax_d:
            c_to = min(d + timedelta(days=chunk_days - 1), dmax_d)
            cdates = [(d + timedelta(days=k)).isoformat() for k in range((c_to - d).days + 1)]
            try:
                s = fetch_sessions(app_id, token, d.isoformat(), c_to.isoformat(), country=True)
            except Exception as e:  # noqa: BLE001 — сбойный чанк: дни уйдут Reporting-only
                print(f"app_dash чанк {d}..{c_to}: ПРОПУЩЕН ({type(e).__name__}: {e})")
                d = c_to + timedelta(days=1)
                continue
            t = aggregate_traffic_channels(s, touches, cdates)
            for iso in cdates:
                channels[iso] = t[iso]
            print(f"app_dash чанк {d}..{c_to}: сессий {len(s)}")
            d = c_to + timedelta(days=1)
    except Exception as e:  # noqa: BLE001
        print(f"app_dash: Logs-сплит ПРОПУЩЕН целиком ({type(e).__name__}: {e}) — тотал в Direct")
    return build_app_traffic_rows(total, channels, dates)


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
