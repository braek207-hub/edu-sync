# Проба BJORN, раунд 6. Оцениваю размер дырки «заход на карточку товара», которую не
# закрывает stat API, и цену её закрытия через Logs API:
#   1. Куда ведут объявления: доля посадочных /product/ (заход на товар без клика по списку)
#      против /catalog/ и прочего — по объявлениям, у которых были показы с 01.08.
#   2. Вес выгрузки Logs API для этого счётчика: доступные поля хитов и оценка объёма
#      (evaluate), без самой выгрузки.
# Read-only: только get/reports Директа и evaluate Метрики (запрос лога не создаётся).
from __future__ import annotations

import hashlib
import json
import os
import time
from collections import Counter
from typing import Any
from urllib.parse import urlparse

import requests

DIRECT_V5 = "https://api.direct.yandex.com/json/v5"
LOGS_URL = "https://api-metrika.yandex.net/management/v1/counter/{counter}/logrequests"

DATE1 = os.environ.get("PROBE_DATE1", "2026-08-01")
DATE2 = os.environ.get("PROBE_DATE2", "2026-08-27")
# Кабинет по решению Павла один — второй (Wildberries-лендинг) не трогаем.
MAIN_LOGIN = os.environ.get("BJORN_MAIN_LOGIN", "audit-bjornlarsen")


def direct_call(service: str, params: dict, login: str, token: str) -> dict:
    resp = requests.post(
        f"{DIRECT_V5}/{service}",
        headers={
            "Authorization": f"Bearer {token}",
            "Client-Login": login,
            "Accept-Language": "ru",
            "Content-Type": "application/json; charset=utf-8",
        },
        data=json.dumps({"method": "get", "params": params}, ensure_ascii=False).encode("utf-8"),
        timeout=180,
    )
    try:
        return resp.json()
    except Exception:
        return {"error": {"error_code": resp.status_code, "error_string": "не JSON", "error_detail": resp.text[:300]}}


def d_err(body: dict) -> str | None:
    e = body.get("error")
    if not e:
        return None
    return f"ERROR {e.get('error_code')} {e.get('error_string')}: {str(e.get('error_detail'))[:300]}"


def direct_report(login: str, token: str, fields: list[str]) -> tuple[int, str]:
    signature = hashlib.md5("|".join(fields + [login, DATE1, DATE2]).encode("utf-8")).hexdigest()[:10]
    body = {
        "params": {
            "SelectionCriteria": {"DateFrom": DATE1, "DateTo": DATE2},
            "FieldNames": fields,
            "ReportName": f"probe6_{login}_{signature}",
            "ReportType": "CUSTOM_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES",
        }
    }
    for _ in range(30):
        resp = requests.post(
            f"{DIRECT_V5}/reports",
            headers={
                "Authorization": f"Bearer {token}",
                "Client-Login": login,
                "Accept-Language": "ru",
                "processingMode": "auto",
                "returnMoneyInMicros": "false",
                "skipReportHeader": "true",
                "skipColumnHeader": "true",
                "skipReportSummary": "true",
                "Content-Type": "application/json; charset=utf-8",
            },
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            timeout=300,
        )
        if resp.status_code in (201, 202):
            time.sleep(10)
            continue
        return resp.status_code, resp.text
    return 0, ""


def clients() -> list[dict[str, str]]:
    default_token = os.environ.get("DIRECT_TOKEN", "")
    out: list[dict[str, str]] = []
    for item in json.loads(os.environ["DIRECT_CLIENTS_JSON"]):
        if isinstance(item, dict):
            login = str(item.get("login") or item.get("client_login") or "").strip()
            token = str(item.get("token") or "").strip() or default_token
            if login:
                out.append({"login": login, "token": token})
        elif isinstance(item, str):
            out.append({"login": item.strip(), "token": default_token})
    return out


def probe_landing_split() -> None:
    print("=" * 78)
    print(f"1. Куда ведут работающие объявления · кабинет {MAIN_LOGIN} · {DATE1}..{DATE2}")
    print("=" * 78)

    client = next((c for c in clients() if c["login"] == MAIN_LOGIN), None)
    if not client:
        print(f"  кабинет {MAIN_LOGIN} не найден в DIRECT_CLIENTS_JSON")
        return
    login, token = client["login"], client["token"]

    # Объявления с показами за период — только они интересны (не весь исторический хлам).
    status, text = direct_report(login, token, ["AdId", "Impressions", "Clicks", "Cost"])
    if status != 200:
        print(f"  отчёт не отдался: HTTP {status} {text[:200]}")
        return
    live: dict[int, tuple[int, int, float]] = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 4 or not parts[0].isdigit():
            continue
        ad_id = int(parts[0])
        impr, clicks = int(parts[1] or 0), int(parts[2] or 0)
        cost = float(parts[3] or 0)
        prev = live.get(ad_id, (0, 0, 0.0))
        live[ad_id] = (prev[0] + impr, prev[1] + clicks, prev[2] + cost)
    live = {k: v for k, v in live.items() if v[0] > 0}
    print(f"  объявлений с показами за период: {len(live)}")

    camps = direct_call("campaigns", {"SelectionCriteria": {}, "FieldNames": ["Id"], "Page": {"Limit": 300}}, login, token)
    if d_err(camps):
        print("  ", d_err(camps))
        return
    camp_ids = [c["Id"] for c in camps["result"].get("Campaigns", [])]

    href_by_ad: dict[int, str] = {}
    image_by_ad: dict[int, str] = {}
    type_by_ad: dict[int, str] = {}
    for start in range(0, len(camp_ids), 10):
        ads = direct_call(
            "ads",
            {
                "SelectionCriteria": {"CampaignIds": camp_ids[start : start + 10]},
                "FieldNames": ["Id", "Type"],
                "TextAdFieldNames": ["Href", "AdImageHash"],
                "TextImageAdFieldNames": ["Href", "AdImageHash"],
                "DynamicTextAdFieldNames": ["AdImageHash"],
                "Page": {"Limit": 10000},
            },
            login,
            token,
        )
        if d_err(ads):
            print("  ", d_err(ads))
            break
        for a in ads["result"].get("Ads", []):
            ad_id = a["Id"]
            type_by_ad[ad_id] = a.get("Type", "?")
            for block in ("TextAd", "TextImageAd", "DynamicTextAd"):
                node = a.get(block) or {}
                if node.get("Href") and ad_id not in href_by_ad:
                    href_by_ad[ad_id] = node["Href"]
                if node.get("AdImageHash") and ad_id not in image_by_ad:
                    image_by_ad[ad_id] = node["AdImageHash"]
        time.sleep(0.2)

    def bucket(href: str) -> str:
        path = urlparse(href).path or "/"
        if path.startswith("/product"):
            return "/product/ (сразу карточка)"
        if path.startswith("/catalog"):
            return "/catalog/ (список)"
        if path in ("/", ""):
            return "главная"
        return f"прочее ({path.split('/')[1][:20]})"

    by_bucket_ads: Counter[str] = Counter()
    by_bucket_clicks: Counter[str] = Counter()
    by_bucket_cost: Counter[str] = Counter()
    no_href = 0
    for ad_id, (impr, clicks, cost) in live.items():
        href = href_by_ad.get(ad_id)
        if not href:
            no_href += 1
            continue
        b = bucket(href)
        by_bucket_ads[b] += 1
        by_bucket_clicks[b] += clicks
        by_bucket_cost[b] += cost

    total_clicks = sum(by_bucket_clicks.values()) or 1
    print(f"  без Href в справочнике (товарные/смарт): {no_href}")
    print(f"\n  {'посадочная':<32} {'объявл.':>8} {'клики':>8} {'доля кликов':>12} {'расход':>12}")
    for b, ads_n in by_bucket_ads.most_common():
        print(f"  {b:<32} {ads_n:>8} {by_bucket_clicks[b]:>8} {by_bucket_clicks[b]/total_clicks*100:>11.1f}% {by_bucket_cost[b]:>12.0f}")

    live_with_image = sum(1 for ad_id in live if ad_id in image_by_ad)
    print(f"\n  из объявлений с показами имеют картинку: {live_with_image} (уникальных картинок {len(set(image_by_ad[a] for a in live if a in image_by_ad))})")
    print(f"  типы объявлений с показами: {Counter(type_by_ad.get(a, '?') for a in live).most_common()}")


def probe_logs_api() -> None:
    print("\n" + "=" * 78)
    print("2. Logs API: доступные поля хитов и оценка веса выгрузки")
    print("=" * 78)
    token = os.environ["METRICA_TOKEN"]
    counter = os.environ["METRICA_COUNTER_ID"]
    headers = {"Authorization": f"OAuth {token}"}

    fields = "ym:pv:watchID,ym:pv:visitID,ym:pv:dateTime,ym:pv:URL,ym:pv:clientID"
    resp = requests.get(
        LOGS_URL.format(counter=counter) + "/evaluate",
        params={"date1": DATE1, "date2": DATE2, "fields": fields, "source": "hits"},
        headers=headers,
        timeout=120,
    )
    print(f"  evaluate hits: HTTP {resp.status_code} {resp.text[:500]}")

    visit_fields = "ym:s:visitID,ym:s:dateTime,ym:s:lastsignTrafficSource,ym:s:lastsignDirectBanner,ym:s:lastsignUTMCampaign"
    resp = requests.get(
        LOGS_URL.format(counter=counter) + "/evaluate",
        params={"date1": DATE1, "date2": DATE2, "fields": visit_fields, "source": "visits"},
        headers=headers,
        timeout=120,
    )
    print(f"  evaluate visits: HTTP {resp.status_code} {resp.text[:500]}")

    resp = requests.get(LOGS_URL.format(counter=counter), headers=headers, timeout=120)
    print(f"  текущие запросы лога: HTTP {resp.status_code} {resp.text[:300]}")


if __name__ == "__main__":
    probe_landing_split()
    probe_logs_api()
