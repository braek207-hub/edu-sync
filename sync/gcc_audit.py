# -*- coding: utf-8 -*-
"""Аудит расхождений Метрика↔GA4 GCC на ДАННЫХ.

Отвечает на: (1) покрытие — на одних ли доменах/устройствах стоят счётчики; (2) какая
метрика к какой ближе; (3) ЧЕМ управляется дневной разрыв (почему скачет 25%↔8%) — раскладка
по устройству и каналу. Печатает parseable-секции, анализ делаем по логу.
"""
import os
from datetime import date, timedelta

import requests

from sync.gcc_ga4 import GA4_CODES, GA4_PROPERTY, HOST_COUNTRY, fetch_ga4_traffic, run_report
from sync.gcc_metrika import domain_country_gcc5

M_URL = "https://api-metrika.yandex.net/stat/v1/data"
COUNTER = os.environ.get("GCC_METRICA_COUNTER_ID") or "98232701"


def _m(dims, metrics, frm, to, filters=None, limit=100000):
    p = {"ids": COUNTER, "date1": frm, "date2": to, "dimensions": ",".join(dims),
         "metrics": ",".join(metrics), "accuracy": "full", "limit": limit}
    if filters:
        p["filters"] = filters
    r = requests.get(M_URL, headers={"Authorization": f"OAuth {os.environ['GCC_METRICA_TOKEN']}"},
                     params=p, timeout=120)
    r.raise_for_status()
    return r.json()["data"]


def _dates(frm, to):
    out, d = [], frm
    while d <= to:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def audit():
    to = date.fromisoformat(os.environ["LIME_GCC_TO"]) if os.environ.get("LIME_GCC_TO") \
        else date.today() - timedelta(days=1)
    frm = date.fromisoformat(os.environ["LIME_GCC_FROM"]) if os.environ.get("LIME_GCC_FROM") \
        else to - timedelta(days=29)
    dates = _dates(frm, to)
    frm_s, to_s = frm.isoformat(), to.isoformat()
    print(f"### AUDIT окно {frm_s}..{to_s} ({len(dates)} дн.)")

    # ── 1. ДНЕВНАЯ ДИНАМИКА: Метрика users vs GA4 totalUsers/activeUsers/sessions (5 стран GCC)
    ga_tot = fetch_ga4_traffic(GA4_PROPERTY, dates, "totalUsers")
    ga_act = fetch_ga4_traffic(GA4_PROPERTY, dates, "activeUsers")
    ga_ses = fetch_ga4_traffic(GA4_PROPERTY, dates, "sessions")
    # Метрика по дням: users по (дата, домен) → фильтр GCC5, сумма
    m_rows = _m(["ym:s:date", "ym:s:startURLDomain"], ["ym:s:users", "ym:s:visits"], frm_s, to_s)
    m_day = {}
    for r in m_rows:
        iso = r["dimensions"][0]["name"]
        if domain_country_gcc5(r["dimensions"][1]["name"]):
            m_day.setdefault(iso, [0, 0])
            m_day[iso][0] += int(r["metrics"][0])
            m_day[iso][1] += int(r["metrics"][1])
    print("\n### DAILY: date | M_users M_visits | GA_total GA_active GA_sessions | d%tot d%act")
    for iso in dates:
        mu, mv = m_day.get(iso, [0, 0])
        gt = ga_tot[iso]["GCC"]["org"] + ga_tot[iso]["GCC"]["paid"]
        gac = ga_act[iso]["GCC"]["org"] + ga_act[iso]["GCC"]["paid"]
        gs = ga_ses[iso]["GCC"]["org"] + ga_ses[iso]["GCC"]["paid"]
        dt = round((gt - mu) / mu * 100, 1) if mu else 0
        da = round((gac - mu) / mu * 100, 1) if mu else 0
        print(f"DAILY {iso} | {mu} {mv} | {gt} {gac} {gs} | {dt} {da}")

    # ── 2. ПОКРЫТИЕ ДОМЕНОВ: где стоят счётчики
    print("\n### COVERAGE Метрика по startURLDomain (users, всё окно, топ):")
    md = _m(["ym:s:startURLDomain"], ["ym:s:users"], frm_s, to_s, limit=50)
    for r in sorted(md, key=lambda x: -int(x["metrics"][0]))[:20]:
        dom = r["dimensions"][0]["name"]
        print(f"MDOM {dom} | {int(r['metrics'][0])} | {domain_country_gcc5(dom) or '—'}")
    print("\n### COVERAGE GA4 по hostName (totalUsers, всё окно, топ):")
    gh = run_report(GA4_PROPERTY, frm_s, to_s, ["hostName"], ("totalUsers",))
    for r in sorted(gh, key=lambda x: -int(x["metrics"][0]))[:20]:
        h = r["dims"][0]
        print(f"GHOST {h} | {int(r['metrics'][0])} | {HOST_COUNTRY.get((h or '').lower(), '—')}")

    # ── 3. УСТРОЙСТВА: доля мобильного + разрыв по устройству (главная гипотеза скачков)
    print("\n### DEVICE Метрика (users, GCC5, всё окно):")
    mdev = _m(["ym:s:deviceCategory", "ym:s:startURLDomain"], ["ym:s:users"], frm_s, to_s)
    magg = {}
    for r in mdev:
        if domain_country_gcc5(r["dimensions"][1]["name"]):
            dev = r["dimensions"][0]["name"]
            magg[dev] = magg.get(dev, 0) + int(r["metrics"][0])
    for dev, u in sorted(magg.items(), key=lambda x: -x[1]):
        print(f"MDEV {dev} | {u}")
    print("\n### DEVICE GA4 (totalUsers, GCC-хосты, всё окно):")
    hosts = list(HOST_COUNTRY.keys())
    gdev = run_report(GA4_PROPERTY, frm_s, to_s, ["deviceCategory", "hostName"], ("totalUsers",))
    gagg = {}
    for r in gdev:
        if (r["dims"][1] or "").lower() in HOST_COUNTRY:
            dev = r["dims"][0]
            gagg[dev] = gagg.get(dev, 0) + int(r["metrics"][0])
    for dev, u in sorted(gagg.items(), key=lambda x: -x[1]):
        print(f"GDEV {dev} | {u}")

    # ── 4. КАНАЛЫ: разрыв по источнику (paid vs organic ведут себя по-разному в системах)
    print("\n### CHANNEL GA4 (totalUsers, GCC-хосты, всё окно):")
    gch = run_report(GA4_PROPERTY, frm_s, to_s, ["sessionDefaultChannelGroup", "hostName"],
                     ("totalUsers",))
    cagg = {}
    for r in gch:
        if (r["dims"][1] or "").lower() in HOST_COUNTRY:
            ch = r["dims"][0]
            cagg[ch] = cagg.get(ch, 0) + int(r["metrics"][0])
    for ch, u in sorted(cagg.items(), key=lambda x: -x[1]):
        print(f"GCH {ch} | {u}")
    print("\n### CHANNEL Метрика (users, GCC5, всё окно):")
    msrc = _m(["ym:s:lastsignTrafficSource", "ym:s:startURLDomain"], ["ym:s:users"], frm_s, to_s)
    sagg = {}
    for r in msrc:
        if domain_country_gcc5(r["dimensions"][1]["name"]):
            s = r["dimensions"][0]["name"]
            sagg[s] = sagg.get(s, 0) + int(r["metrics"][0])
    for s, u in sorted(sagg.items(), key=lambda x: -x[1]):
        print(f"MCH {s} | {u}")

    print("\n### AUDIT DONE")
