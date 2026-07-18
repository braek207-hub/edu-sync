"""Зонд контрактов GCC: Triple Whale (Data-Out) + GA4 (Data API).

Discovery-скрипт (B1 плана LIME GCC). НЕ пишет в БД — только печатает сырые
ответы, чтобы зафиксировать реальные форматы полей для клиентов B2/B3.

Запуск (из d:\\vscode\\edu-sync, .env подхватывается автоматически):
    python scripts/probe_gcc_apis.py tw       # Triple Whale summary-page
    python scripts/probe_gcc_apis.py ga4      # GA4 трафик по каналам
    python scripts/probe_gcc_apis.py both
    python scripts/probe_gcc_apis.py domain   # P1: Метрика — dimension домена (страны GCC)
    python scripts/probe_gcc_apis.py journey  # P3: TW journey — страна заказа
    python scripts/probe_gcc_apis.py savejourney tests/fixtures/tw_orders_journey_sample.json

Env:
    GCC_TRIPLEWHALE_API_KEY   — ключ TW (scopes Read)
    GCC_TW_SHOP_DOMAIN        — *.myshopify.com магазина GCC (для summary-page)
    GCC_GA4_PROPERTY_ID       — id ресурса GA4 (default 417919368)
    GOOGLE_SERVICE_ACCOUNT / GOOGLE_APPLICATION_CREDENTIALS — сервис-аккаунт (реюз GSC)
"""
import json
import os
import sys
from datetime import date, timedelta

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

TW_URL = "https://api.triplewhale.com/api/v2/summary-page/get-data"
GA4_PROPERTY_DEFAULT = "417919368"
GA4_SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]


def _yesterday_range():
    y = date.today() - timedelta(days=1)
    return y.isoformat(), y.isoformat()


def probe_tw():
    key = (os.environ.get("GCC_TRIPLEWHALE_API_KEY") or "").strip()
    shop = (os.environ.get("GCC_TW_SHOP_DOMAIN") or "").strip()
    if not key:
        print("SKIP tw: нет GCC_TRIPLEWHALE_API_KEY")
        return
    start, end = _yesterday_range()
    body = {
        "shopDomain": shop,               # *.myshopify.com
        "period": {"start": start, "end": end},
        "todayHour": 25,
    }
    print(f"[tw] POST {TW_URL}\n[tw] body={json.dumps(body)}")
    resp = requests.post(
        TW_URL,
        headers={"x-api-key": key, "Content-Type": "application/json"},
        json=body,
        timeout=60,
    )
    print(f"[tw] HTTP {resp.status_code}")
    try:
        data = resp.json()
    except Exception:
        print("[tw] non-JSON body:", resp.text[:2000])
        return
    if isinstance(data, dict) and isinstance(data.get("metrics"), list):
        print(f"[tw] summary-page metrics ({len(data['metrics'])}):")
        for m in data["metrics"]:
            vals = m.get("values") or {}
            svc = ",".join(m.get("services") or [])
            print(f"  - {m.get('metricId','?'):<24} {m.get('title','?'):<28} "
                  f"cur={vals.get('current')} prev={vals.get('previous')} [{svc}] {m.get('type','')}")
        print("[tw] top-level keys:", list(data.keys()))
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2)[:4000])


def probe_ga4():
    pid = (os.environ.get("GCC_GA4_PROPERTY_ID") or GA4_PROPERTY_DEFAULT).strip()
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        creds = Credentials.from_service_account_file(
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"], scopes=GA4_SCOPES
        )
    else:
        creds = Credentials.from_service_account_info(
            json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT"]), scopes=GA4_SCOPES
        )
    svc = build("analyticsdata", "v1beta", credentials=creds, cache_discovery=False)
    start, end = _yesterday_range()
    body = {
        "dateRanges": [{"startDate": start, "endDate": end}],
        # hostName — чтобы увидеть, делится ли трафик по доменам стран GCC
        # (ae./bh./kw./sa./qa./om.) для будущего разбиения region=gcc на страны.
        "dimensions": [{"name": "date"}, {"name": "hostName"}, {"name": "sessionSourceMedium"}],
        "metrics": [
            {"name": "sessions"},
            {"name": "totalUsers"},
            {"name": "newUsers"},
            {"name": "bounceRate"},
        ],
        "limit": 50,
    }
    print(f"[ga4] runReport properties/{pid}\n[ga4] body={json.dumps(body)}")
    resp = svc.properties().runReport(property=f"properties/{pid}", body=body).execute()
    print(json.dumps(resp, ensure_ascii=False, indent=2)[:6000])


def probe_tw_attr():
    """Attribution: заказы с журналами атрибуции (канальная разбивка выручки/заказов)."""
    key = (os.environ.get("GCC_TRIPLEWHALE_API_KEY") or "").strip()
    shop = (os.environ.get("GCC_TW_SHOP_DOMAIN") or "").strip()
    if not key:
        print("SKIP attr: нет GCC_TRIPLEWHALE_API_KEY")
        return
    url = "https://api.triplewhale.com/api/v2/attribution/get-orders-with-journeys-v2"
    start, end = _yesterday_range()
    # Контракт из офиц. примера TW: поле "shop" (не shopDomain), без "model".
    body = {
        "shop": shop,
        "startDate": start,
        "endDate": end,
        "excludeJourneyData": True,
    }
    print(f"[attr] POST {url}\n[attr] body={json.dumps(body)}")
    resp = requests.post(
        url,
        headers={"x-api-key": key, "content-type": "application/json"},
        json=body,
        timeout=90,
    )
    print(f"[attr] HTTP {resp.status_code}")
    try:
        data = resp.json()
    except Exception:
        print("[attr] non-JSON:", resp.text[:1500]); return
    if not isinstance(data, dict):
        print(json.dumps(data, ensure_ascii=False, indent=2)[:1500]); return
    print("[attr] top-level keys:", list(data.keys()),
          "| totalForRange:", data.get("totalForRange"), "count:", data.get("count"))
    orders = data.get("ordersWithJourneys") or []
    print(f"[attr] ordersWithJourneys: {len(orders)}")
    if orders:
        o = orders[0]
        print("[attr] first order keys:", list(o.keys()))
        print("[attr] currency:", o.get("currency"), "total_price:", o.get("total_price"))
        attr = o.get("attribution") or {}
        print("[attr] attribution models:", list(attr.keys()))
        for model in ("lastClick", "lastPlatformClick"):
            print(f"[attr] {model} sample:", json.dumps(attr.get(model), ensure_ascii=False)[:400])
    # Распределение заказов/выручки по источнику (lastPlatformClick → fallback lastClick → 'direct/none')
    from collections import defaultdict
    dist = defaultdict(lambda: {"orders": 0, "revenue": 0.0})
    for o in orders:
        a = o.get("attribution") or {}
        src = None
        for model in ("lastPlatformClick", "lastClick", "fullLastClick"):
            arr = a.get(model) or []
            if arr and isinstance(arr, list) and arr[0].get("source"):
                src = arr[0]["source"]; break
        key = src or "(direct/none)"
        dist[key]["orders"] += 1
        dist[key]["revenue"] += float(o.get("total_price") or 0)
    print("[attr] распределение заказов по источнику:")
    for k, v in sorted(dist.items(), key=lambda x: -x[1]["orders"]):
        print(f"   {k:<22} orders={v['orders']:<4} revenue={round(v['revenue'])} AED")


def probe_metrika():
    """Проверить доступ к счётчику Метрики GCC (98232701) и получить пробу трафика."""
    counter = (os.environ.get("GCC_METRICA_COUNTER_ID") or "98232701").strip()
    token = ""
    for name in ("GCC_METRICA_TOKEN", "LIME_METRICA_TOKEN", "LIME_DIRECT_TOKEN",
                 "POLINAREPIK_YANDEX_TOKEN"):
        v = (os.environ.get(name) or "").strip()
        if v:
            token = v
            print(f"[metrika] использую токен из {name}")
            break
    if not token:
        print("[metrika] SKIP: нет токена (LIME_DIRECT_TOKEN / GCC_METRICA_TOKEN)")
        return
    hdr = {"Authorization": f"OAuth {token}"}
    # 1) Management API — доступ к счётчику
    m = requests.get(
        f"https://api-metrika.yandex.net/management/v1/counter/{counter}",
        headers=hdr, timeout=30,
    )
    print(f"[metrika] management/counter/{counter} HTTP {m.status_code}")
    if m.status_code == 200:
        c = m.json().get("counter", {})
        print(f"[metrika] counter: name={c.get('name')} site={c.get('site')} status={c.get('status')}")
    else:
        print("[metrika] management body:", m.text[:400])
    # 2) Stat API — проба трафика по источнику за вчера
    start, end = _yesterday_range()
    params = {
        "ids": counter,
        "date1": start, "date2": end,
        "metrics": "ym:s:visits,ym:s:users,ym:s:pageviews,ym:s:bounceRate",
        "dimensions": "ym:s:date,ym:s:lastsignTrafficSource,ym:s:lastsignSourceEngine",
        "accuracy": "full", "limit": 50,
    }
    s = requests.get(
        "https://api-metrika.yandex.net/stat/v1/data", headers=hdr, params=params, timeout=60,
    )
    print(f"[metrika] stat/v1/data HTTP {s.status_code}")
    try:
        d = s.json()
    except Exception:
        print("[metrika] stat non-JSON:", s.text[:600]); return
    if "data" in d:
        print(f"[metrika] rows={len(d['data'])} totals={d.get('totals')}")
        for row in d["data"][:30]:
            dims = " / ".join((x.get("name") or "—") for x in row.get("dimensions", []))
            print(f"   {dims}: {row.get('metrics')}")
    else:
        print("[metrika] stat body:", json.dumps(d, ensure_ascii=False)[:600])


def save_metrika_fixture(path: str):
    """Сохранить обрезанный сырой ответ Metrika Stat API как фикстуру для теста B2."""
    counter = (os.environ.get("GCC_METRICA_COUNTER_ID") or "98232701").strip()
    token = ""
    for name in ("GCC_METRICA_TOKEN", "LIME_METRICA_TOKEN", "LIME_DIRECT_TOKEN"):
        v = (os.environ.get(name) or "").strip()
        if v:
            token = v; break
    if not token:
        print("SKIP savefix: нет токена"); return
    start, end = _yesterday_range()
    params = {
        "ids": counter, "date1": start, "date2": end,
        "metrics": "ym:s:visits,ym:s:users,ym:s:pageviews,ym:s:bounceRate",
        "dimensions": "ym:s:date,ym:s:lastsignTrafficSource,ym:s:lastsignSourceEngine",
        "accuracy": "full", "limit": 100,
    }
    r = requests.get("https://api-metrika.yandex.net/stat/v1/data",
                     headers={"Authorization": f"OAuth {token}"}, params=params, timeout=60)
    d = r.json()
    if isinstance(d.get("data"), list):
        d["data"] = d["data"][:10]  # обрезать до 10 строк
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    print(f"saved fixture ({len(d.get('data', []))} rows) → {path}")


def save_tw_fixtures(orders_path: str, spend_path: str):
    """Сохранить обрезанные фикстуры TW: заказы (attribution, без PII) и метрики расхода."""
    key = (os.environ.get("GCC_TRIPLEWHALE_API_KEY") or "").strip()
    shop = (os.environ.get("GCC_TW_SHOP_DOMAIN") or "").strip()
    start, end = _yesterday_range()
    hdr = {"x-api-key": key, "content-type": "application/json"}
    # Orders
    r = requests.post(
        "https://api.triplewhale.com/api/v2/attribution/get-orders-with-journeys-v2",
        headers=hdr, json={"shop": shop, "startDate": start, "endDate": end, "excludeJourneyData": True},
        timeout=90)
    d = r.json()
    orders = d.get("ordersWithJourneys") or []
    trimmed = []
    for o in orders[:12]:
        trimmed.append({
            "order_id": "REDACTED", "total_price": o.get("total_price"),
            "currency": o.get("currency"), "created_at": o.get("created_at"),
            "attribution": {k: (o.get("attribution") or {}).get(k)
                            for k in ("lastPlatformClick", "lastClick", "fullLastClick")},
        })
    with open(orders_path, "w", encoding="utf-8") as f:
        json.dump({"ordersWithJourneys": trimmed, "totalForRange": len(orders),
                   "count": len(orders), "finishedRange": True}, f, ensure_ascii=False, indent=2)
    print(f"saved orders fixture ({len(trimmed)} of {len(orders)}) → {orders_path}")
    # Summary spend metrics
    r2 = requests.post(
        "https://api.triplewhale.com/api/v2/summary-page/get-data",
        headers=hdr, json={"shopDomain": shop, "period": {"start": start, "end": end}, "todayHour": 25},
        timeout=60)
    d2 = r2.json()
    want = {"ga_adCost", "fb_ads_spend", "totalSnapchatSpend", "totalOrders", "totalSales",
            "totalTiktokSpend", "totalPinterestSpend", "totalBingSpend"}
    metrics = [m for m in (d2.get("metrics") or []) if m.get("metricId") in want]
    with open(spend_path, "w", encoding="utf-8") as f:
        json.dump({"metrics": metrics}, f, ensure_ascii=False, indent=2)
    print(f"saved spend fixture ({len(metrics)} metrics) → {spend_path}")


def probe_metrika_domain():
    """P1: какой dimension делит трафик GCC по доменам стран (ae./sa./kw./qa./om./bh.)."""
    counter = (os.environ.get("GCC_METRICA_COUNTER_ID") or "98232701").strip()
    token = (os.environ.get("GCC_METRICA_TOKEN") or "").strip()
    if not token:
        print("[domain] SKIP: нет GCC_METRICA_TOKEN")
        return
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=6)
    for dim in ("ym:s:startURLDomain", "ym:s:URLDomain"):
        params = {
            "ids": counter, "date1": start.isoformat(), "date2": end.isoformat(),
            "metrics": "ym:s:visits,ym:s:users", "dimensions": dim,
            "accuracy": "full", "limit": 100,
        }
        r = requests.get("https://api-metrika.yandex.net/stat/v1/data",
                         headers={"Authorization": f"OAuth {token}"}, params=params, timeout=60)
        print(f"\n[domain] {dim} HTTP {r.status_code}")
        d = r.json()
        if "data" not in d:
            print("[domain]", json.dumps(d, ensure_ascii=False)[:400])
            continue
        print(f"[domain] rows={len(d['data'])} totals={d.get('totals')}")
        for row in d["data"]:
            print(f"   {(row['dimensions'][0].get('name') or '—'):<28} {row['metrics']}")


def probe_tw_journey():
    """P3: журнал тачпоинтов заказа → какой домен (страна) определяет заказ."""
    import re
    from collections import Counter

    key = (os.environ.get("GCC_TRIPLEWHALE_API_KEY") or "").strip()
    shop = (os.environ.get("GCC_TW_SHOP_DOMAIN") or "").strip()
    if not key:
        print("[journey] SKIP: нет GCC_TRIPLEWHALE_API_KEY")
        return
    start, end = _yesterday_range()
    r = requests.post(
        "https://api.triplewhale.com/api/v2/attribution/get-orders-with-journeys-v2",
        headers={"x-api-key": key, "content-type": "application/json"},
        json={"shop": shop, "startDate": start, "endDate": end, "excludeJourneyData": False},
        timeout=120)
    print(f"[journey] HTTP {r.status_code}")
    orders = (r.json() or {}).get("ordersWithJourneys") or []
    print(f"[journey] заказов: {len(orders)}")
    if not orders:
        return
    host_re = re.compile(r"https?://([^/]+)", re.I)
    gcc = ("ae", "bh", "kw", "sa", "qa", "om")

    def prefixes(order):
        out = []
        for tp in order.get("journey") or []:
            m = host_re.match(tp.get("path") or "")
            p = m.group(1).lower().split(".")[0] if m else None
            if p in gcc:
                out.append(p)
        return out

    events = Counter()
    for o in orders:
        for tp in o.get("journey") or []:
            events[tp.get("event")] += 1
    print(f"[journey] события: {dict(events)}")
    j0 = orders[0].get("journey") or []
    print(f"[journey] порядок: [0]={j0[0].get('time') if j0 else '—'} "
          f"[-1]={j0[-1].get('time') if j0 else '—'} (ожидаем убывание времени)")

    dist, mixed, disagree, none = Counter(), 0, 0, 0
    for o in orders:
        ps = prefixes(o)
        if not ps:
            none += 1
            dist[None] += 1
            continue
        if len(set(ps)) > 1:
            mixed += 1
        if ps[0] != Counter(ps).most_common(1)[0][0]:
            disagree += 1
        dist[ps[0]] += 1
    print(f"[journey] смешанных доменов: {mixed} | last != dominant: {disagree} | без страны: {none}")
    print(f"[journey] распределение по правилу last-touchpoint: {dict(dist)}")


def save_journey_fixture(path: str):
    """Сохранить компактную фикстуру заказов с journey (PII вырезан, journey до 6 тачпоинтов)."""
    import re
    from collections import Counter

    key = (os.environ.get("GCC_TRIPLEWHALE_API_KEY") or "").strip()
    shop = (os.environ.get("GCC_TW_SHOP_DOMAIN") or "").strip()
    start, end = _yesterday_range()
    r = requests.post(
        "https://api.triplewhale.com/api/v2/attribution/get-orders-with-journeys-v2",
        headers={"x-api-key": key, "content-type": "application/json"},
        json={"shop": shop, "startDate": start, "endDate": end, "excludeJourneyData": False},
        timeout=120)
    orders = (r.json() or {}).get("ordersWithJourneys") or []
    host_re = re.compile(r"https?://([^/]+)", re.I)
    gcc = ("ae", "bh", "kw", "sa", "qa", "om")

    def country_of(order):
        for tp in order.get("journey") or []:
            m = host_re.match(tp.get("path") or "")
            p = m.group(1).lower().split(".")[0] if m else None
            if p in gcc:
                return p
        return "none"

    picked, seen = [], Counter()
    for o in orders:
        c = country_of(o)
        if seen[c] >= 2 or len(picked) >= 10:
            continue
        seen[c] += 1
        trimmed = {k: v for k, v in o.items() if k not in ("customer_id", "email", "customer")}
        trimmed["order_id"] = "REDACTED"
        trimmed["order_name"] = "REDACTED"
        trimmed["journey"] = (o.get("journey") or [])[:6]
        picked.append(trimmed)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ordersWithJourneys": picked, "totalForRange": len(orders),
                   "count": len(orders), "finishedRange": True}, f, ensure_ascii=False, indent=2)
    print(f"saved {len(picked)} orders (страны {dict(seen)}) → {path}")


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    if which == "savejourney":
        save_journey_fixture(sys.argv[2])
        sys.exit(0)
    if which == "domain":
        probe_metrika_domain()
        sys.exit(0)
    if which == "journey":
        probe_tw_journey()
        sys.exit(0)
    if which == "savetw":
        save_tw_fixtures(sys.argv[2], sys.argv[3])
        sys.exit(0)
    if which == "savefix":
        save_metrika_fixture(sys.argv[2])
        sys.exit(0)
    if which == "metrika":
        probe_metrika()
        sys.exit(0)
    if which in ("tw", "both"):
        probe_tw()
    if which in ("attr", "both"):
        probe_tw_attr()
    if which in ("ga4", "both"):
        probe_ga4()
