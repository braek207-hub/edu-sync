#!/usr/bin/env python3
"""Как PROCONTEXT атрибутирует app-сессии кампании VK — проверка на одном дне (только чтение).

Гипотеза Павла: сессия → кампания по диплинку, без диплинка → источник установки.
Строка PROCONTEXT 27807511 за 05.09.2026: 271 сессия, 98 юзеров, 4 заказа / 44 789.
Строка `vk-ads-(ex.-mytarget)` (not set) за 05.09: 2 756 сессий, 932 юзера, 46 / 491 351.

Считаем из AppMetrica за тот же день:
  A. диплинки с параметрами кампании C: события, устройства, сессии этих устройств,
     покупки этих устройств в день D — сравнить со строкой кампании;
  B. сессии устройств, установленных с паблишера VK (первая установка в окне), у которых
     в день D НЕ было диплинка — сравнить со строкой (not set);
  C. установки дня D по кампании C: сколько переатрибуций, и покупки этих устройств
     до дня D / в день D / после — проверка «старые покупатели приписаны когорте».

ENV: APPMETRICA_TOKEN, DATABASE_URL (справочник VK), PROBE_DAY (default 2026-09-05),
PROBE_CAMPAIGN (default 27807511), PROBE_INSTALLS_FROM (default 2026-03-01).
"""
import os
from collections import defaultdict
from datetime import date, datetime, timedelta

from sync.appmetrica_logs import fetch_export, fetch_installations, fetch_purchase_events, fetch_sessions
from sync.lime_appmetrica import (
    _truthy, first_install_attribution, install_campaign, is_vk_publisher, load_vk_entity_map,
    month_chunks, param_of, parse_dt, purchase_facts,
)

APP_ID = os.environ.get("APPMETRICA_APP_ID") or "4415407"
DAY = (os.environ.get("PROBE_DAY") or "").strip() or "2026-09-05"
CAMPAIGN = (os.environ.get("PROBE_CAMPAIGN") or "").strip() or "27807511"
INSTALLS_FROM = (os.environ.get("PROBE_INSTALLS_FROM") or "").strip() or "2026-03-01"
DEEPLINK_FIELDS = ("appmetrica_device_id,tracker_name,tracking_id,publisher_name,"
                   "deeplink_url_parameters,event_datetime")


def main():
    token = os.environ["APPMETRICA_TOKEN"]
    entity_map = load_vk_entity_map()
    d = date.fromisoformat(DAY)
    print(f"[probe] день {DAY}, кампания {CAMPAIGN}, справочник VK: {len(entity_map)} записей", flush=True)

    # ── A. диплинки дня ────────────────────────────────────────────────────────
    deeplinks = fetch_export("deeplinks", APP_ID, token, DAY, DAY, DEEPLINK_FIELDS)
    print(f"A. диплинков за день: {len(deeplinks)}", flush=True)
    dl_by_campaign: dict[str, set] = defaultdict(set)
    dl_events: dict[str, int] = defaultdict(int)
    dl_devices_any: set = set()
    sample = ""
    for r in deeplinks:
        dev = r.get("appmetrica_device_id") or ""
        params = r.get("deeplink_url_parameters") or ""
        pub = r.get("publisher_name") or ""
        c = install_campaign(pub, params, entity_map) or param_of(params, "utm_campaign")
        dl_events[c or "(без кампании)"] += 1
        if dev:
            dl_by_campaign[c or "(без кампании)"].add(dev)
            dl_devices_any.add(dev)
        if c == CAMPAIGN and not sample:
            sample = f"{pub} | {r.get('tracker_name')} | {params[:160]}"
    top = sorted(dl_events.items(), key=lambda kv: -kv[1])[:8]
    print("   топ кампаний по диплинкам: " + "; ".join(f"{k}={v}" for k, v in top))
    dl_dev = dl_by_campaign.get(CAMPAIGN, set())
    print(f"   кампания {CAMPAIGN}: диплинков {dl_events.get(CAMPAIGN, 0)}, устройств {len(dl_dev)}")
    print(f"   пример: {sample}")

    # ── сессии и покупки дня ───────────────────────────────────────────────────
    sessions = fetch_sessions(APP_ID, token, DAY, DAY)
    sess_by_dev: dict[str, int] = defaultdict(int)
    for s in sessions:
        dev = s.get("appmetrica_device_id")
        if dev:
            sess_by_dev[dev] += 1
    print(f"   сессий за день всего: {len(sessions)}, устройств {len(sess_by_dev)}", flush=True)
    day_purchases = purchase_facts(fetch_purchase_events(
        APP_ID, token, DAY, DAY, "purchase", date_dimension="default"))
    buy_by_dev: dict[str, list] = defaultdict(list)
    for dev, dt, _txn, amount in day_purchases:
        buy_by_dev[dev].append(amount)

    def agg(devs: set) -> str:
        sess = sum(sess_by_dev.get(x, 0) for x in devs)
        users = sum(1 for x in devs if sess_by_dev.get(x))
        orders = sum(len(buy_by_dev.get(x, ())) for x in devs)
        rev = sum(sum(buy_by_dev.get(x, ())) for x in devs)
        return f"сессий {sess}, устройств с сессией {users}, заказов {orders}, выручка {rev:.0f}"

    print(f"   устройства с диплинком {CAMPAIGN}: {agg(dl_dev)}")
    print(f"   ↳ PROCONTEXT строка кампании 05.09: 271 сессия, 98 юзеров, 4 заказа, 44 789", flush=True)

    # ── B. установки за окно → первая установка устройства ─────────────────────
    installs = fetch_installations(APP_ID, token, INSTALLS_FROM, DAY)
    first = first_install_attribution(installs, True, False, entity_map)
    print(f"B. установок {INSTALLS_FROM}..{DAY}: {len(installs)}, устройств {len(first)}", flush=True)
    vk_devs = {dev for dev, (dt, pub, _det, _c) in first.items() if is_vk_publisher(pub)}
    c_devs = {dev for dev, (dt, pub, _det, c) in first.items() if c == CAMPAIGN}
    vk_no_dl = {x for x in vk_devs if x in sess_by_dev and x not in dl_devices_any}
    vk_with_dl = {x for x in vk_devs if x in sess_by_dev and x in dl_devices_any}
    print(f"   устройства с установкой VK (любая кампания) и сессией в день D: {len(vk_devs & set(sess_by_dev))}")
    print(f"   из них БЕЗ диплинка в день D: {agg(vk_no_dl)}")
    print(f"   ↳ PROCONTEXT строка vk-ads-(ex.-mytarget) (not set) 05.09: 2 756 сессий, 932 юзера, 46 заказов, 491 351")
    print(f"   из них С диплинком в день D: {agg(vk_with_dl)}")
    print(f"   устройства с первой установкой из {CAMPAIGN} (все даты): {agg(c_devs)}")
    print(f"   ↳ наша витрина install_source 05.09: 213 сессий, 3 заказа, 27 596", flush=True)

    # ── C. установки дня D по кампании C: переатрибуции и история покупок ──────
    day_inst = [r for r in installs
                if parse_dt(r["install_datetime"]).date() == d
                and install_campaign(r.get("publisher_name") or "", r.get("click_url_parameters") or "", entity_map) == CAMPAIGN]
    devs_c = {r.get("appmetrica_device_id") for r in day_inst if r.get("appmetrica_device_id")}
    reattr = sum(1 for r in day_inst if _truthy(r.get("is_reattribution")))
    reinst = sum(1 for r in day_inst if _truthy(r.get("is_reinstallation")))
    print(f"C. установок {CAMPAIGN} за {DAY}: строк {len(day_inst)}, устройств {len(devs_c)}, "
          f"is_reattribution={reattr}, is_reinstallation={reinst}", flush=True)
    earlier = {dev: dt for dev, (dt, _p, _d, _c) in first.items() if dev in devs_c and dt.date() < d}
    print(f"   из них уже ставили приложение раньше в окне (первая установка < D): {len(earlier)}")
    before = after = same = 0
    rev_before = rev_after = rev_same = 0.0
    top_dev: dict[str, list] = defaultdict(lambda: [0, 0.0])
    for since, until in month_chunks(INSTALLS_FROM, (d + timedelta(days=6)).isoformat()):
        facts = purchase_facts(fetch_purchase_events(APP_ID, token, since, until, "purchase",
                                                     date_dimension="default"))
        for dev, dt, _txn, amount in facts:
            if dev not in devs_c:
                continue
            top_dev[dev][0] += 1
            top_dev[dev][1] += amount
            if dt.date() < d:
                before += 1; rev_before += amount
            elif dt.date() == d:
                same += 1; rev_same += amount
            else:
                after += 1; rev_after += amount
        print(f"   покупки {since}..{until} обработаны", flush=True)
    print(f"   покупки этих устройств: ДО {DAY}: {before} на {rev_before:.0f}; в день: {same} на {rev_same:.0f}; "
          f"после (6 дн.): {after} на {rev_after:.0f}")
    heavy = sorted(top_dev.items(), key=lambda kv: -kv[1][0])[:5]
    print("   топ устройств по заказам: " + "; ".join(f"{k[-6:]}…={v[0]} зак./{v[1]:.0f}" for k, v in heavy))
    print(f"   ↳ старая когорта 05.09 в lime_app_installs: 52 установки, 96 заказов, 740 965")


if __name__ == "__main__":
    main()
