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
    # Метрика платный по дням (Ad traffic, GCC5) — проверить, что разрыв идёт за долей платного
    mp_rows = _m(["ym:s:date", "ym:s:startURLDomain"], ["ym:s:users"], frm_s, to_s,
                 filters="ym:s:lastsignTrafficSource=='ad'")
    m_paid = {}
    for r in mp_rows:
        iso = r["dimensions"][0]["name"]
        if domain_country_gcc5(r["dimensions"][1]["name"]):
            m_paid[iso] = m_paid.get(iso, 0) + int(r["metrics"][0])
    print("\n### DAILY: date | M_users | GA_total GA_active | d%tot d%act | M_paid GA_paid paid%GA")
    for iso in dates:
        mu, mv = m_day.get(iso, [0, 0])
        gt = ga_tot[iso]["GCC"]["org"] + ga_tot[iso]["GCC"]["paid"]
        gac = ga_act[iso]["GCC"]["org"] + ga_act[iso]["GCC"]["paid"]
        gp = ga_tot[iso]["GCC"]["paid"]
        mp = m_paid.get(iso, 0)
        dt = round((gt - mu) / mu * 100, 1) if mu else 0
        da = round((gac - mu) / mu * 100, 1) if mu else 0
        psh = round(gp / gt * 100, 1) if gt else 0
        print(f"DAILY {iso} | {mu} | {gt} {gac} | {dt} {da} | {mp} {gp} {psh}")

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

    hf = {"filter": {"fieldName": "hostName",
                     "inListFilter": {"values": list(HOST_COUNTRY.keys())}}}

    # ── 5. ИСТОЧНИК/МЕДИУМ детально — ГДЕ именно перевес (Google Ads vs Meta)
    print("\n### SRC GA4 sessionSource/sessionMedium (totalUsers, GCC-хосты, топ 25):")
    gsm = run_report(GA4_PROPERTY, frm_s, to_s, ["sessionSource", "sessionMedium"],
                     ("totalUsers",), hf)
    for r in sorted(gsm, key=lambda x: -int(x["metrics"][0]))[:25]:
        print(f"GSRC {r['dims'][0]} / {r['dims'][1]} | {int(r['metrics'][0])}")
    print("\n### SRC Метрика площадка платного lastsignSourceEngine (users, GCC5 ad, топ 25):")
    men = _m(["ym:s:lastsignSourceEngine", "ym:s:startURLDomain"], ["ym:s:users"], frm_s, to_s,
             filters="ym:s:lastsignTrafficSource=='ad'")
    eagg = {}
    for r in men:
        if domain_country_gcc5(r["dimensions"][1]["name"]):
            e = r["dimensions"][0]["name"]
            eagg[e] = eagg.get(e, 0) + int(r["metrics"][0])
    for e, u in sorted(eagg.items(), key=lambda x: -x[1])[:25]:
        print(f"MSRC {e} | {u}")

    # ── 6. ПЛАТФОРМА ПО ДНЯМ: Google Ads vs Meta — где разрыв и как скачет
    def _g_plat(src, med):
        s, m = (src or "").lower(), (med or "").lower()
        if "google" in s and any(x in m for x in ("cpc", "paid", "ppc")):
            return "google"
        if "cross" in m:  # PMax cross-network
            return "google"
        if any(x in s for x in ("facebook", "instagram", "fb", "ig", "meta")) and \
           any(x in m for x in ("cpc", "paid", "ppc", "social")):
            return "meta"
        return None
    # БЕЗ dimension_filter (с date+фильтром API давал 0) — фильтруем hostName в коде, как в v1.
    # Google=Paid Search+Cross-network(PMax), Meta=Paid Social.
    gpd = run_report(GA4_PROPERTY, frm_s, to_s,
                     ["date", "sessionDefaultChannelGroup", "hostName"], ("totalUsers",))
    g_goog, g_meta = {}, {}
    for r in gpd:
        if (r["dims"][2] or "").lower() not in HOST_COUNTRY:
            continue
        dr = r["dims"][0]
        d0 = f"{dr[:4]}-{dr[4:6]}-{dr[6:8]}"  # GA4 отдаёт YYYYMMDD → ISO (иначе ключи не сходятся)
        ch = r["dims"][1]
        if ch in ("Paid Search", "Cross-network"):
            g_goog[d0] = g_goog.get(d0, 0) + int(r["metrics"][0])
        elif ch == "Paid Social":
            g_meta[d0] = g_meta.get(d0, 0) + int(r["metrics"][0])
    mpd = _m(["ym:s:date", "ym:s:lastsignSourceEngine", "ym:s:startURLDomain"], ["ym:s:users"],
             frm_s, to_s, filters="ym:s:lastsignTrafficSource=='ad'")
    m_goog, m_meta = {}, {}
    for r in mpd:
        if not domain_country_gcc5(r["dimensions"][2]["name"]):
            continue
        e = (r["dimensions"][1]["name"] or "").lower()
        iso = r["dimensions"][0]["name"]
        if "google" in e:
            m_goog[iso] = m_goog.get(iso, 0) + int(r["metrics"][0])
        elif any(x in e for x in ("instagram", "facebook", "meta")):
            m_meta[iso] = m_meta.get(iso, 0) + int(r["metrics"][0])
    print("\n### PLATFORM date | M_google GA_google d%g | M_meta GA_meta d%m")
    for iso in dates:
        mg, gg = m_goog.get(iso, 0), g_goog.get(iso, 0)
        mm, gm = m_meta.get(iso, 0), g_meta.get(iso, 0)
        dg = round((gg - mg) / mg * 100, 1) if mg else 0
        dm = round((gm - mm) / mm * 100, 1) if mm else 0
        print(f"PLAT {iso} | {mg} {gg} {dg} | {mm} {gm} {dm}")

    # ── 7. ПОКРЫТИЕ ЛЕНДИНГОВ: не ловит ли GA4 входы на страницах, где тега Метрики нет
    print("\n### LANDING GA4 (sessions по landingPage, GCC-хосты):")
    gl = run_report(GA4_PROPERTY, frm_s, to_s, ["landingPage"], ("sessions", "totalUsers"), hf)
    print(f"GLND_STAT distinct={len(gl)} sessions={sum(int(r['metrics'][0]) for r in gl)}")
    for r in sorted(gl, key=lambda x: -int(x["metrics"][0]))[:20]:
        print(f"GLND {r['dims'][0][:70]} | s={int(r['metrics'][0])} u={int(r['metrics'][1])}")
    print("\n### LANDING Метрика (visits по startURLPath, GCC5):")
    ml = _m(["ym:s:startURLPath", "ym:s:startURLDomain"], ["ym:s:visits", "ym:s:users"], frm_s, to_s)
    mlagg = {}
    for r in ml:
        if domain_country_gcc5(r["dimensions"][1]["name"]):
            path = r["dimensions"][0]["name"]
            a = mlagg.setdefault(path, [0, 0])
            a[0] += int(r["metrics"][0])
            a[1] += int(r["metrics"][1])
    print(f"MLND_STAT distinct={len(mlagg)} visits={sum(v[0] for v in mlagg.values())}")
    for path, v in sorted(mlagg.items(), key=lambda x: -x[1][0])[:20]:
        print(f"MLND {path[:70]} | v={v[0]} u={v[1]}")

    # ── 8. КЛИКИ Google Ads/Meta (из TW ads_table) vs Метрика DAU
    print("\n### CLICKS TW ads_table по каналу:")
    try:
        from sync.gcc_tw_ads import tw_sql
        tk, shop = os.environ.get("GCC_TRIPLEWHALE_API_KEY"), os.environ.get("GCC_TW_SHOP_DOMAIN")
        q = ("SELECT channel, SUM(clicks) AS clicks, SUM(impressions) AS impressions, "
             "SUM(spend) AS spend FROM ads_table "
             "WHERE event_date BETWEEN @startDate AND @endDate GROUP BY channel")
        for r in tw_sql(tk, shop, q, frm_s, to_s):
            print(f"TWCL {r.get('channel')} | clicks={r.get('clicks')} "
                  f"impr={r.get('impressions')} spend={r.get('spend')}")
    except Exception as e:  # noqa: BLE001
        print(f"TWCL ERR {type(e).__name__}: {e}")

    # Клик ≈ СЕССИЯ, не DAU. Сверяем клики с сессиями GA4 и визитами Метрики (не с users!).
    gses = run_report(GA4_PROPERTY, frm_s, to_s, ["sessionSource", "sessionMedium"],
                      ("sessions",), hf)
    gg_s, gm_s = 0, 0
    for r in gses:
        p = _g_plat(r["dims"][0], r["dims"][1])
        if p == "google":
            gg_s += int(r["metrics"][0])
        elif p == "meta":
            gm_s += int(r["metrics"][0])
    mvis = _m(["ym:s:lastsignSourceEngine", "ym:s:startURLDomain"], ["ym:s:visits"], frm_s, to_s,
              filters="ym:s:lastsignTrafficSource=='ad'")
    mg_v, mm_v = 0, 0
    for r in mvis:
        if not domain_country_gcc5(r["dimensions"][1]["name"]):
            continue
        e = (r["dimensions"][0]["name"] or "").lower()
        if "google" in e:
            mg_v += int(r["metrics"][0])
        elif any(x in e for x in ("instagram", "facebook", "meta")):
            mm_v += int(r["metrics"][0])
    print(f"SESS Google | GA4_sessions={gg_s} Metrika_visits={mg_v}")
    print(f"SESS Meta   | GA4_sessions={gm_s} Metrika_visits={mm_v}")
    for r in sorted(gses, key=lambda x: -int(x["metrics"][0]))[:10]:
        print(f"GSESS_SRC {r['dims'][0]}/{r['dims'][1]} | {int(r['metrics'][0])}")

    # ── 9. КОНВЕРСИЯ по каналу: заказы (TW в lime_stats) / Метрика DAU
    print("\n### CONV lime_stats платный: channel/subchannel | users orders CR% | clicks(лид)")
    import psycopg2
    conn = psycopg2.connect(os.environ["DATABASE_URL"].split("?")[0], connect_timeout=30)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT channel, subchannel, SUM(users)::int, SUM(purchases_count)::int, "
                "ROUND(SUM(purchases_revenue)::numeric,0) "
                "FROM lime_stats WHERE region='gcc' AND data_source='web' "
                "AND traffic_type='Платный' AND date BETWEEN %s AND %s "
                "GROUP BY channel, subchannel ORDER BY 3 DESC", (frm_s, to_s))
            for ch, sc, u, o, rev in cur.fetchall():
                cr = round(o / u * 100, 2) if u else 0
                print(f"CONV {ch}/{sc} | u={u} o={o} cr={cr}% rev={rev}")
    finally:
        conn.close()

    print("\n### AUDIT DONE")


def page_data():
    """Данные для страницы «старый vs новый подход» — одно окно, обе методики.

    Считает на ОДНОМ 30-дн окне и одних данных, чтобы разница была на 100% из-за метода:
      • Трафик-знаменатель: GA4 users по группам каналов (старый) vs Метрика DAU (новый).
      • Заказы-числитель: linearAll (старый, дробно 1/N) vs lastPlatformClick (новый, весь заказ)
        — оба из ОДНОГО TW-ответа, чтобы тотал совпал, а разошлось лишь распределение.
      • Конверсия платного: заказы/DAU в обеих связках.
    Печатает parseable-секции PD_*, страницу строим по логу.
    """
    from collections import defaultdict

    from sync.gcc_channels import map_tw_source
    from sync.gcc_triplewhale import fetch_tw_orders, order_touchpoint

    to = date.fromisoformat(os.environ["LIME_GCC_TO"]) if os.environ.get("LIME_GCC_TO") \
        else date.today() - timedelta(days=1)
    frm = date.fromisoformat(os.environ["LIME_GCC_FROM"]) if os.environ.get("LIME_GCC_FROM") \
        else to - timedelta(days=29)
    frm_s, to_s = frm.isoformat(), to.isoformat()
    print(f"### PAGE-DATA окно {frm_s}..{to_s}")

    hosts = set(HOST_COUNTRY.keys())

    # ── ТРАФИК GA4 по группам каналов (GCC-хосты, всё окно) ──
    def _paid_group(g):
        return g in ("Paid Search", "Paid Social", "Cross-network", "Display",
                     "Paid Shopping", "Paid Other")
    gch = run_report(GA4_PROPERTY, frm_s, to_s,
                     ["sessionDefaultChannelGroup", "hostName"], ("totalUsers", "activeUsers"))
    ga_grp_tot, ga_grp_act = defaultdict(int), defaultdict(int)
    for r in gch:
        if (r["dims"][1] or "").lower() not in hosts:
            continue
        g = r["dims"][0]
        ga_grp_tot[g] += int(r["metrics"][0])
        ga_grp_act[g] += int(r["metrics"][1])
    ga_total = sum(ga_grp_tot.values())
    ga_act_total = sum(ga_grp_act.values())
    ga_paid = sum(v for g, v in ga_grp_tot.items() if _paid_group(g))
    ga_goog = ga_grp_tot.get("Paid Search", 0) + ga_grp_tot.get("Cross-network", 0)
    ga_meta = ga_grp_tot.get("Paid Social", 0)
    print("\n### PD_GA4 group | totalUsers | activeUsers")
    for g, v in sorted(ga_grp_tot.items(), key=lambda x: -x[1]):
        print(f"PD_GA4 {g} | {v} | {ga_grp_act.get(g, 0)}")
    print(f"PD_GA4TOT total={ga_total} active={ga_act_total} paid={ga_paid} "
          f"google={ga_goog} meta={ga_meta}")

    # ── ТРАФИК Метрика DAU (GCC5, всё окно) ──
    mall = _m(["ym:s:startURLDomain"], ["ym:s:users"], frm_s, to_s, limit=200)
    m_total = sum(int(r["metrics"][0]) for r in mall
                  if domain_country_gcc5(r["dimensions"][0]["name"]))
    mpaid = _m(["ym:s:lastsignSourceEngine", "ym:s:startURLDomain"], ["ym:s:users"], frm_s, to_s,
               filters="ym:s:lastsignTrafficSource=='ad'")
    m_paid = m_goog = m_meta = 0
    m_paid_eng = defaultdict(int)
    for r in mpaid:
        if not domain_country_gcc5(r["dimensions"][1]["name"]):
            continue
        e = (r["dimensions"][0]["name"] or "").lower()
        u = int(r["metrics"][0])
        m_paid += u
        m_paid_eng[e or "∅"] += u
        if "google" in e:
            m_goog += u
        elif any(x in e for x in ("instagram", "facebook", "meta")):
            m_meta += u
    print(f"\n### PD_M total={m_total} paid={m_paid} google={m_goog} meta={m_meta} "
          f"organic={m_total - m_paid}")
    for e, u in sorted(m_paid_eng.items(), key=lambda x: -x[1])[:12]:
        print(f"PD_MENG {e} | {u}")

    # ── ЗАКАЗЫ: linearAll vs lastPlatformClick из одного TW-ответа ──
    tw = fetch_tw_orders(os.environ["GCC_TRIPLEWHALE_API_KEY"],
                         os.environ["GCC_TW_SHOP_DOMAIN"], frm_s, to_s)
    lpc_o, lpc_r = defaultdict(float), defaultdict(float)   # lastPlatformClick (новый)
    lin_o, lin_r = defaultdict(float), defaultdict(float)   # linearAll (старый)
    tt_lpc_o, tt_lin_o = defaultdict(float), defaultdict(float)  # по traffic_type
    tt_lpc_r, tt_lin_r = defaultdict(float), defaultdict(float)
    # дневные платные заказы обеих моделей — для таблицы-приложения и дневной CR
    day_lpc_paid, day_lin_paid = defaultdict(float), defaultdict(float)
    dkey = None
    if tw:
        for cand in ("created_at", "createdAt", "date", "order_date", "orderDate",
                     "processed_at", "createdAtShopTz"):
            v = tw[0].get(cand)
            if isinstance(v, str) and v[:3] == "202":
                dkey = cand
                break
        print(f"PD_ORDKEYS dkey={dkey} keys={sorted(tw[0].keys())}")
    total_orders = 0
    total_rev = 0.0
    for o in tw:
        total_orders += 1
        rev = float(o.get("total_price") or 0)
        total_rev += rev
        iso_o = (o.get(dkey) or "")[:10] if dkey else None
        # НОВЫЙ: весь заказ последнему платформенному клику
        tp = order_touchpoint(o)
        ch, sc, tt = map_tw_source(tp.get("source") or None, (tp.get("campaignId") or "").strip() or None)
        key = f"{ch}/{sc}"
        lpc_o[key] += 1
        lpc_r[key] += rev
        tt_lpc_o[tt] += 1
        tt_lpc_r[tt] += rev
        if tt == "Платный" and iso_o:
            day_lpc_paid[iso_o] += 1
        # СТАРЫЙ: linearAll — 1/N на касание
        la = (o.get("attribution") or {}).get("linearAll") or []
        if la:
            w = 1.0 / len(la)
            for t in la:
                ch2, sc2, tt2 = map_tw_source(t.get("source") or None,
                                              (t.get("campaignId") or "").strip() or None)
                k2 = f"{ch2}/{sc2}"
                lin_o[k2] += w
                lin_r[k2] += rev * w
                tt_lin_o[tt2] += w
                tt_lin_r[tt2] += rev * w
                if tt2 == "Платный" and iso_o:
                    day_lin_paid[iso_o] += w
        else:  # нет журнала — оба метода кладут в тот же lpc-бакет
            lin_o[key] += 1
            lin_r[key] += rev
            tt_lin_o[tt] += 1
            tt_lin_r[tt] += rev
            if tt == "Платный" and iso_o:
                day_lin_paid[iso_o] += 1
    print(f"\n### PD_ORDTOT orders={total_orders} revenue={total_rev:.0f}")
    print("### PD_ORD channel | last_orders last_rev | linear_orders linear_rev")
    for k in sorted(set(lpc_o) | set(lin_o), key=lambda x: -(lpc_o.get(x, 0) + lin_o.get(x, 0))):
        print(f"PD_ORD {k} | {lpc_o.get(k,0):.1f} {lpc_r.get(k,0):.0f} | "
              f"{lin_o.get(k,0):.1f} {lin_r.get(k,0):.0f}")
    print("### PD_TT traffic_type | last_orders last_rev | linear_orders linear_rev")
    for tt in sorted(set(tt_lpc_o) | set(tt_lin_o)):
        print(f"PD_TT {tt} | {tt_lpc_o.get(tt,0):.1f} {tt_lpc_r.get(tt,0):.0f} | "
              f"{tt_lin_o.get(tt,0):.1f} {tt_lin_r.get(tt,0):.0f}")

    # ── КОНВЕРСИЯ платного: заказы платные / DAU платные, обе связки ──
    lpc_paid_o = tt_lpc_o.get("Платный", 0)
    lin_paid_o = tt_lin_o.get("Платный", 0)
    print("\n### PD_CONV method | paid_orders | paid_DAU | CR%")
    if m_paid:
        print(f"PD_CONV НОВЫЙ(last/Метрика) | {lpc_paid_o:.0f} | {m_paid} | "
              f"{lpc_paid_o / m_paid * 100:.2f}")
    if ga_paid:
        print(f"PD_CONV СТАРЫЙ(linear/GA4) | {lin_paid_o:.0f} | {ga_paid} | "
              f"{lin_paid_o / ga_paid * 100:.2f}")
    # промежуточные комбинации — изолировать вклад каждого сдвига
    if ga_paid:
        print(f"PD_CONV last/GA4 | {lpc_paid_o:.0f} | {ga_paid} | {lpc_paid_o / ga_paid * 100:.2f}")
    if m_paid:
        print(f"PD_CONV linear/Метрика | {lin_paid_o:.0f} | {m_paid} | {lin_paid_o / m_paid * 100:.2f}")

    dates = _dates(frm, to)

    # ── ДНЕВНЫЕ РЯДЫ ТРАФИКА: обе системы, тотал и платный ──
    ga_day = fetch_ga4_traffic(GA4_PROPERTY, dates, "totalUsers")
    md_rows = _m(["ym:s:date", "ym:s:startURLDomain"], ["ym:s:users"], frm_s, to_s)
    md_tot = defaultdict(int)
    for r in md_rows:
        if domain_country_gcc5(r["dimensions"][1]["name"]):
            md_tot[r["dimensions"][0]["name"]] += int(r["metrics"][0])
    mdp_rows = _m(["ym:s:date", "ym:s:startURLDomain"], ["ym:s:users"], frm_s, to_s,
                  filters="ym:s:lastsignTrafficSource=='ad'")
    md_paid = defaultdict(int)
    for r in mdp_rows:
        if domain_country_gcc5(r["dimensions"][1]["name"]):
            md_paid[r["dimensions"][0]["name"]] += int(r["metrics"][0])
    print("\n### PD_DAY iso | m_total m_paid | ga_total ga_paid | lin_paid_ord lpc_paid_ord")
    for iso in dates:
        g = ga_day.get(iso, {}).get("GCC", {"org": 0, "paid": 0})
        print(f"PD_DAY {iso} | {md_tot.get(iso, 0)} {md_paid.get(iso, 0)} | "
              f"{g['org'] + g['paid']} {g['paid']} | "
              f"{day_lin_paid.get(iso, 0):.1f} {day_lpc_paid.get(iso, 0):.0f}")

    # ── ПЛАТФОРМЫ ПО ДНЯМ: Google/Meta в обеих системах (как audit §6) ──
    gpd = run_report(GA4_PROPERTY, frm_s, to_s,
                     ["date", "sessionDefaultChannelGroup", "hostName"], ("totalUsers",))
    g_goog_d, g_meta_d = defaultdict(int), defaultdict(int)
    for r in gpd:
        if (r["dims"][2] or "").lower() not in hosts:
            continue
        dr = r["dims"][0]
        d0 = f"{dr[:4]}-{dr[4:6]}-{dr[6:8]}"
        chg = r["dims"][1]
        if chg in ("Paid Search", "Cross-network"):
            g_goog_d[d0] += int(r["metrics"][0])
        elif chg == "Paid Social":
            g_meta_d[d0] += int(r["metrics"][0])
    mpd = _m(["ym:s:date", "ym:s:lastsignSourceEngine", "ym:s:startURLDomain"], ["ym:s:users"],
             frm_s, to_s, filters="ym:s:lastsignTrafficSource=='ad'")
    m_goog_d, m_meta_d = defaultdict(int), defaultdict(int)
    for r in mpd:
        if not domain_country_gcc5(r["dimensions"][2]["name"]):
            continue
        e = (r["dimensions"][1]["name"] or "").lower()
        iso = r["dimensions"][0]["name"]
        if "google" in e:
            m_goog_d[iso] += int(r["metrics"][0])
        elif any(x in e for x in ("instagram", "facebook", "meta")):
            m_meta_d[iso] += int(r["metrics"][0])
    print("\n### PD_PLATD iso | m_google ga_google | m_meta ga_meta")
    for iso in dates:
        print(f"PD_PLATD {iso} | {m_goog_d.get(iso, 0)} {g_goog_d.get(iso, 0)} | "
              f"{m_meta_d.get(iso, 0)} {g_meta_d.get(iso, 0)}")

    # ── КЛИКИ КАБИНЕТОВ (TW ads_table) — внешний якорь, тоталы и по дням ──
    try:
        from sync.gcc_tw_ads import tw_sql
        tk, shop = os.environ["GCC_TRIPLEWHALE_API_KEY"], os.environ["GCC_TW_SHOP_DOMAIN"]
        rows = tw_sql(tk, shop,
                      "SELECT event_date, channel, SUM(clicks) AS clicks, SUM(spend) AS spend "
                      "FROM ads_table WHERE event_date BETWEEN @startDate AND @endDate "
                      "GROUP BY event_date, channel", frm_s, to_s)
        clk_tot, clk_g_d, clk_m_d = defaultdict(float), defaultdict(float), defaultdict(float)
        spend_tot = defaultdict(float)
        for r in rows:
            chn = (r.get("channel") or "?").lower()
            iso = str(r.get("event_date") or "")[:10]
            c = float(r.get("clicks") or 0)
            clk_tot[chn] += c
            spend_tot[chn] += float(r.get("spend") or 0)
            if "google" in chn:
                clk_g_d[iso] += c
            elif "facebook" in chn or "meta" in chn:
                clk_m_d[iso] += c
        print("\n### PD_CLK channel | clicks | spend")
        for chn, c in sorted(clk_tot.items(), key=lambda x: -x[1]):
            print(f"PD_CLK {chn} | {c:.0f} | {spend_tot[chn]:.0f}")
        print("### PD_CLKD iso | google_clicks | meta_clicks")
        for iso in dates:
            print(f"PD_CLKD {iso} | {clk_g_d.get(iso, 0):.0f} | {clk_m_d.get(iso, 0):.0f}")
    except Exception as e:  # noqa: BLE001
        print(f"PD_CLK ERR {type(e).__name__}: {e}")

    # ── СЕССИИ/ВИЗИТЫ vs КЛИКИ (то же окно): проверка на уровне посещений ──
    def _g_plat2(src, med):
        s, m2 = (src or "").lower(), (med or "").lower()
        if "google" in s and any(x in m2 for x in ("cpc", "paid", "ppc")):
            return "google"
        if "cross" in m2:
            return "google"
        if any(x in s for x in ("facebook", "instagram", "fb", "ig", "meta")) and \
           any(x in m2 for x in ("cpc", "paid", "ppc", "social")):
            return "meta"
        return None
    hf = {"filter": {"fieldName": "hostName",
                     "inListFilter": {"values": list(HOST_COUNTRY.keys())}}}
    gses = run_report(GA4_PROPERTY, frm_s, to_s, ["sessionSource", "sessionMedium"],
                      ("sessions",), hf)
    gg_s = sum(int(r["metrics"][0]) for r in gses if _g_plat2(r["dims"][0], r["dims"][1]) == "google")
    gm_s = sum(int(r["metrics"][0]) for r in gses if _g_plat2(r["dims"][0], r["dims"][1]) == "meta")
    mvis = _m(["ym:s:lastsignSourceEngine", "ym:s:startURLDomain"], ["ym:s:visits"], frm_s, to_s,
              filters="ym:s:lastsignTrafficSource=='ad'")
    mg_v = mm_v = 0
    for r in mvis:
        if not domain_country_gcc5(r["dimensions"][1]["name"]):
            continue
        e = (r["dimensions"][0]["name"] or "").lower()
        if "google" in e:
            mg_v += int(r["metrics"][0])
        elif any(x in e for x in ("instagram", "facebook", "meta")):
            mm_v += int(r["metrics"][0])
    print(f"\nPD_SESS google | ga4_sessions={gg_s} m_visits={mg_v}")
    print(f"PD_SESS meta | ga4_sessions={gm_s} m_visits={mm_v}")

    print("\n### PAGE-DATA DONE")


def ru_direct_check():
    """RU: клики Яндекс.Директа (lime_direct_stats) vs Метрика DAU/визиты (lime_metrika_campaign_ru).

    Проверка родной пары Метрика↔Директ (аналог GA4↔Google Ads): насколько Метрика ловит
    свою рекламу. Всё из БД, без API.
    """
    import psycopg2
    to = date.fromisoformat(os.environ["LIME_GCC_TO"]) if os.environ.get("LIME_GCC_TO") \
        else date.today() - timedelta(days=1)
    frm = date.fromisoformat(os.environ["LIME_GCC_FROM"]) if os.environ.get("LIME_GCC_FROM") \
        else to - timedelta(days=29)
    conn = psycopg2.connect(os.environ["DATABASE_URL"].split("?")[0], connect_timeout=30)
    with conn.cursor() as cur:
        cur.execute("SELECT date::text, SUM(clicks)::int FROM lime_direct_stats "
                    "WHERE date BETWEEN %s AND %s GROUP BY date", (frm, to))
        clk = dict(cur.fetchall())
        cur.execute("SELECT date::text, SUM(users)::int, SUM(visits)::int "
                    "FROM lime_metrika_campaign_ru WHERE traffic_type='Платный' "
                    "AND subchannel ILIKE %s AND date BETWEEN %s AND %s GROUP BY date",
                    ("%Директ%", frm, to))
        mu = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        cur.execute("SELECT subchannel, SUM(users)::int FROM lime_metrika_campaign_ru "
                    "WHERE traffic_type='Платный' AND date BETWEEN %s AND %s "
                    "GROUP BY subchannel ORDER BY 2 DESC", (frm, to))
        brk = cur.fetchall()
    conn.close()
    print(f"### RU платные подканалы Метрики: {brk}")
    print(f"### RU date | direct_clicks | metrika_visits | metrika_DAU | visits/clicks% | DAU/clicks%")
    tc = tv = tu = 0
    d = frm
    while d <= to:
        iso = d.isoformat()
        c = clk.get(iso, 0)
        u, v = mu.get(iso, (0, 0))
        tc += c; tv += v; tu += u
        vc = round(v / c * 100, 1) if c else 0
        uc = round(u / c * 100, 1) if c else 0
        print(f"RU {iso} | {c} | {v} | {u} | {vc} | {uc}")
        d += timedelta(days=1)
    print(f"RU TOTAL | clicks={tc} visits={tv} DAU={tu} | visits/clicks={round(tv/tc*100,1) if tc else 0}% "
          f"DAU/clicks={round(tu/tc*100,1) if tc else 0}%")
