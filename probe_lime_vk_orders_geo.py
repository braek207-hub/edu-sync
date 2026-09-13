#!/usr/bin/env python3
"""Почему у нас заказов установок больше, чем gross PROCONTEXT, у свежей VK-кампании (только чтение).

Кейс 30072646 «APP: Хиты продаж, 09/26», 31.08–06.09.2026: PROCONTEXT 3 заказа / 38 195,
у нас 5 / 78 193 (после фикса). Обе стороны атрибутируют по установке. Гипотеза: PROCONTEXT
фильтрует по стране RU (витрина region='ru'), наши витрины страну не смотрят.

Печатает по каждому устройству кампании: дата/страна установки, покупки с датой, страной,
суммой и transaction_id — чтобы увидеть, какие 2 заказа PROCONTEXT не считает.

ENV: APPMETRICA_TOKEN, DATABASE_URL, PROBE_CAMPAIGN (30072646), PROBE_FROM (2026-09-01),
PROBE_TO (2026-09-06).
"""
import json
import os
from collections import defaultdict

from sync.appmetrica_logs import fetch_installations, fetch_purchase_events
from sync.lime_appmetrica import install_campaign, load_vk_entity_map, parse_dt, purchase_facts

APP_ID = os.environ.get("APPMETRICA_APP_ID") or "4415407"
CAMPAIGN = (os.environ.get("PROBE_CAMPAIGN") or "").strip() or "30072646"
FROM = (os.environ.get("PROBE_FROM") or "").strip() or "2026-09-01"
TO = (os.environ.get("PROBE_TO") or "").strip() or "2026-09-06"


def main():
    token = os.environ["APPMETRICA_TOKEN"]
    entity_map = load_vk_entity_map()
    installs = fetch_installations(APP_ID, token, FROM, TO, country=True)
    mine: dict[str, list] = defaultdict(list)
    for r in installs:
        c = install_campaign(r.get("publisher_name") or "", r.get("click_url_parameters") or "", entity_map)
        if c == CAMPAIGN and r.get("appmetrica_device_id"):
            mine[r["appmetrica_device_id"]].append(
                (r["install_datetime"], r.get("country_iso_code") or "?", r.get("city") or "",
                 r.get("is_reattribution"), r.get("is_reinstallation")))
    print(f"[probe] {CAMPAIGN} {FROM}..{TO}: строк установок {sum(len(v) for v in mine.values())}, "
          f"устройств {len(mine)}", flush=True)
    by_country: dict[str, int] = defaultdict(int)
    for v in mine.values():
        by_country[v[0][1]] += 1
    print("   страны установок: " + ", ".join(f"{k}={n}" for k, n in sorted(by_country.items(), key=lambda kv: -kv[1])))

    events = fetch_purchase_events(APP_ID, token, FROM, TO, "purchase", country=True, date_dimension="default")
    geo = {}
    for e in events:
        dev = e.get("appmetrica_device_id")
        if dev in mine:
            geo[(dev, e.get("event_datetime"))] = (e.get("country_iso_code") or "?", e.get("city") or "")
    facts = [f for f in purchase_facts(events) if f[0] in mine]
    print(f"   сырых purchase-событий у устройств кампании: {sum(1 for e in events if e.get('appmetrica_device_id') in mine)}, "
          f"фактов после дедупа по transaction_id: {len(facts)}")
    print("   device | установка (дата, страна, город, reattr, reinst) | покупка (дата, страна, город, сумма, txn)")
    for dev, dt, txn, amount in sorted(facts, key=lambda f: f[1]):
        inst = mine[dev][0]
        g = geo.get((dev, dt.strftime("%Y-%m-%d %H:%M:%S")), ("?", ""))
        print(f"   …{dev[-6:]} | {inst[0]} {inst[1]} {inst[2]} r={inst[3]} ri={inst[4]} | "
              f"{dt} {g[0]} {g[1]} {amount:.0f} {txn}")
    raw = [e for e in events if e.get("appmetrica_device_id") in mine]
    if raw:
        ej = raw[0].get("event_json") or ""
        print(f"   пример event_json: {json.dumps(json.loads(ej), ensure_ascii=False)[:400] if ej else ''}")


if __name__ == "__main__":
    main()
