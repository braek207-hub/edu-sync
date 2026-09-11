#!/usr/bin/env python3
"""Два списка «починить у источника» из аудита RU 11.09.2026 (только чтение).

1. Кампании Директа, чьи клики приходят на сайт БЕЗ utm_campaign: у PROCONTEXT такие
   сессии лежат как `ya_direct/ad` без кампании (146 тыс. за месяц). Метрика знает
   кампанию по клику (ym:s:lastsignDirectClickOrder) — считаем по каждой кампании долю
   визитов без UTM. Кампании с долей >0 — включить в них разметку UTM в Директе.

2. Трекеры AppMetrica, у которых в параметрах ссылки остался литеральный макрос
   `{utm_source}` (95 тыс. сессий приложения RT в аудите): группируем установки по
   tracker_name/tracking_id, где click_url_parameters содержит `{utm_source}`.

ENV: LIME_METRIKA_TOKEN, APPMETRICA_TOKEN, APPMETRICA_APP_ID (4415407), PROBE_FROM/TO.
"""
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

from sync.appmetrica_logs import fetch_export

API_URL = "https://api-metrika.yandex.net/stat/v1/data"
COUNTER = "23504302"

TO = (os.environ.get("PROBE_TO") or "").strip() or (
    datetime.now(timezone.utc) - timedelta(days=2)
).date().isoformat()
FROM = (os.environ.get("PROBE_FROM") or "").strip() or (
    datetime.fromisoformat(TO) - timedelta(days=13)
).date().isoformat()


def metrika_rows(dimensions, filters, limit=10000):
    params = {
        "ids": COUNTER,
        "date1": FROM,
        "date2": TO,
        "metrics": "ym:s:visits,ym:s:ecommercePurchases",
        "dimensions": ",".join(dimensions),
        "filters": f"ym:s:regionCountryName=='Russia' AND ({filters})",
        "sort": "-ym:s:visits",
        "accuracy": "full",
        "limit": limit,
    }
    r = requests.get(API_URL, headers={"Authorization": f"OAuth {os.environ['LIME_METRIKA_TOKEN']}"},
                     params=params, timeout=120)
    r.raise_for_status()
    js = r.json()
    print(f"   строк {js.get('total_rows')}, визитов {js.get('totals', [0])[0]}")
    return js.get("data", [])


def direct_without_utm():
    print("\n── 1. Кампании Директа с кликами без utm_campaign (Метрика, AdvEngine=ya_direct) ──")
    rows = metrika_rows(
        ("ym:s:lastsignDirectClickOrder", "ym:s:lastsignDirectClickOrderName",
         "ym:s:lastsignUTMCampaign"),
        "ym:s:lastsignAdvEngine=='ya_direct'",
    )
    acc = defaultdict(lambda: {"name": "", "visits": 0, "no_utm": 0, "orders_no_utm": 0.0})
    for it in rows:
        order, order_name, utm = it["dimensions"]
        oid = order.get("id") or order.get("name") or "(без заказа)"
        v, p = it["metrics"]
        a = acc[oid]
        a["name"] = order_name.get("name") or ""
        a["visits"] += int(v)
        if not utm.get("name"):
            a["no_utm"] += int(v)
            a["orders_no_utm"] += float(p)
    bad = sorted((k, a) for k, a in acc.items() if a["no_utm"] > 0)
    bad.sort(key=lambda kv: -kv[1]["no_utm"])
    total_no = sum(a["no_utm"] for _, a in bad)
    print(f"   кампаний с кликами без UTM: {len(bad)}, визитов без UTM: {total_no}")
    print("   campaign_id | без UTM | всего | доля | заказов без UTM | имя")
    for oid, a in bad:
        share = a["no_utm"] / a["visits"] if a["visits"] else 0
        print(f"   {oid} | {a['no_utm']} | {a['visits']} | {share:.0%} | "
              f"{int(a['orders_no_utm'])} | {a['name']}")


def trackers_with_raw_macro():
    print("\n── 2. Трекеры AppMetrica с литеральным {utm_source} в click_url_parameters ──")
    token = os.environ.get("APPMETRICA_TOKEN")
    if not token:
        print("   APPMETRICA_TOKEN не задан — пропуск")
        return
    app_id = os.environ.get("APPMETRICA_APP_ID") or "4415407"
    rows = fetch_export(
        "installations", app_id, token, FROM, TO,
        "tracker_name,tracking_id,publisher_name,click_url_parameters,install_datetime",
    )
    print(f"   установок за окно: {len(rows)}")
    acc = defaultdict(lambda: {"installs": 0, "raw": 0, "sample": ""})
    for r in rows:
        key = (r.get("tracking_id") or "", r.get("tracker_name") or "", r.get("publisher_name") or "")
        a = acc[key]
        a["installs"] += 1
        params = r.get("click_url_parameters") or ""
        if "{utm_source}" in params or "%7Butm_source%7D" in params:
            a["raw"] += 1
            if not a["sample"]:
                a["sample"] = params[:160]
    bad = sorted(((k, a) for k, a in acc.items() if a["raw"] > 0), key=lambda kv: -kv[1]["raw"])
    print(f"   трекеров с сырым макросом: {len(bad)}")
    print("   tracking_id | трекер | паблишер | установок | с {utm_source} | пример параметров")
    for (tid, name, pub), a in bad:
        print(f"   {tid} | {name} | {pub} | {a['installs']} | {a['raw']} | {a['sample']}")


def _truthy(v):
    return (v or "").strip().lower() in ("1", "true", "yes")


def _raw_macro_by_tracker(endpoint: str, fields: str, param_field: str, days: int):
    """Клики/диплинки трекеров (в т.ч. ремаркетинг — RT-атрибуция) с сырым макросом.
    Окно один день: 5 дней кликов убили раннер (SIGTERM 143 на 6-й минуте, память)."""
    token = os.environ.get("APPMETRICA_TOKEN")
    if not token:
        return
    app_id = os.environ.get("APPMETRICA_APP_ID") or "4415407"
    since = (datetime.fromisoformat(TO) - timedelta(days=days - 1)).date().isoformat()
    print(f"\n── {endpoint}: сырой {{utm_source}} по трекерам, {since}..{TO} ──")
    rows = fetch_export(endpoint, app_id, token, since, TO, fields)
    print(f"   строк: {len(rows)}", flush=True)
    acc = defaultdict(lambda: {"n": 0, "raw": 0, "sample": ""})
    for r in rows:
        key = (r.get("tracking_id") or "", r.get("tracker_name") or "", r.get("publisher_name") or "")
        a = acc[key]
        a["n"] += 1
        params = r.get(param_field) or ""
        if "{utm_source}" in params or "%7Butm_source%7D" in params:
            a["raw"] += 1
            if not a["sample"]:
                a["sample"] = params[:200]
    bad = sorted(((k, a) for k, a in acc.items() if a["raw"] > 0), key=lambda kv: -kv[1]["raw"])
    print(f"   трекеров с сырым макросом: {len(bad)}")
    for (tid, name, pub), a in bad:
        print(f"   {tid} | {name} | {pub} | всего {a['n']} | с макросом {a['raw']} | {a['sample']}")


def main():
    print(f"[probe] окно {FROM} .. {TO}", flush=True)
    if not _truthy(os.environ.get("PROBE_SKIP_PART1")):
        direct_without_utm()
        trackers_with_raw_macro()
    if not _truthy(os.environ.get("PROBE_ONLY_DEEPLINKS")):
        # 10.09.2026: 2,03 млн кликов за день, сырого макроса 0.
        _raw_macro_by_tracker(
            "clicks", "tracker_name,tracking_id,publisher_name,click_url_parameters,click_datetime",
            "click_url_parameters", 1)
    _raw_macro_by_tracker(
        "deeplinks", "tracker_name,tracking_id,publisher_name,deeplink_url_parameters,event_datetime",
        "deeplink_url_parameters", 3)


if __name__ == "__main__":
    main()
