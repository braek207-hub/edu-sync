# Проба BJORN, раунд 7. Раунд 6 показал: за август работали 219 объявлений, живых картинок
# в библиотеке Директа всего 4, а 171 объявление из отчёта не нашлось в ads.get. Похоже,
# трафик идёт товарными кампаниями, где креатив = карточка товара из фида. Закрываю:
#   1. Что это за «потерянные» AdId — тип объявления при запросе по Ids со всеми FieldNames.
#   2. Есть ли товарный фид и что в нём (feeds.get) — источник картинок товаров.
#   3. Правильные имена полей визитов в Logs API, чтобы сшить хиты с объявлением/источником.
# Read-only.
from __future__ import annotations

import hashlib
import json
import os
import time
from collections import Counter
from typing import Any

import requests

DIRECT_V5 = "https://api.direct.yandex.com/json/v5"
LOGS_URL = "https://api-metrika.yandex.net/management/v1/counter/{counter}/logrequests"
DATE1 = os.environ.get("PROBE_DATE1", "2026-08-01")
DATE2 = os.environ.get("PROBE_DATE2", "2026-08-27")
MAIN_LOGIN = os.environ.get("BJORN_MAIN_LOGIN", "audit-bjornlarsen")


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
    sig = hashlib.md5("|".join(fields + [login, DATE1, DATE2]).encode("utf-8")).hexdigest()[:10]
    body = {
        "params": {
            "SelectionCriteria": {"DateFrom": DATE1, "DateTo": DATE2},
            "FieldNames": fields,
            "ReportName": f"probe7_{login}_{sig}",
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


def main() -> None:
    client = next((c for c in clients() if c["login"] == MAIN_LOGIN), None)
    if not client:
        print(f"кабинет {MAIN_LOGIN} не найден")
        return
    login, token = client["login"], client["token"]

    print("=" * 78)
    print(f"1. Природа работающих объявлений · {MAIN_LOGIN} · {DATE1}..{DATE2}")
    print("=" * 78)

    status, text = direct_report(login, token, ["AdId", "CampaignId", "CampaignName", "AdFormat", "Impressions", "Clicks", "Cost"])
    if status != 200:
        print(f"  отчёт не отдался HTTP {status}: {text[:200]}")
        return
    rows: dict[int, dict[str, Any]] = {}
    for line in text.splitlines():
        p = line.split("\t")
        if len(p) < 7 or not p[0].isdigit():
            continue
        ad_id = int(p[0])
        r = rows.setdefault(ad_id, {"camp": p[1], "camp_name": p[2], "format": p[3], "impr": 0, "clicks": 0, "cost": 0.0})
        r["impr"] += int(p[4] or 0)
        r["clicks"] += int(p[5] or 0)
        r["cost"] += float(p[6] or 0)
    live = {k: v for k, v in rows.items() if v["impr"] > 0}
    print(f"  объявлений с показами: {len(live)}")
    print(f"  по AdFormat: {Counter(v['format'] for v in live.values()).most_common()}")
    top = sorted(live.items(), key=lambda kv: -kv[1]["clicks"])[:10]
    print(f"\n  {'AdId':<22} {'формат':<16} {'клики':>7} {'расход':>10}  кампания")
    for ad_id, v in top:
        print(f"  {ad_id:<22} {v['format'][:16]:<16} {v['clicks']:>7} {v['cost']:>10.0f}  {v['camp_name'][:38]}")

    print("\n--- 1b. Запрос ads.get прямо по этим Ids, все FieldNames ---")
    ids = [ad_id for ad_id, _ in top]
    ads = direct_call(
        "ads",
        {
            "SelectionCriteria": {"Ids": ids},
            "FieldNames": ["Id", "CampaignId", "AdGroupId", "Type", "Subtype", "State"],
            "TextAdFieldNames": ["Title", "Href", "AdImageHash"],
            "TextImageAdFieldNames": ["AdImageHash", "Href"],
            "DynamicTextAdFieldNames": ["Text", "AdImageHash"],
            "SmartAdBuilderAdFieldNames": ["Creative"],
            "Page": {"Limit": 50},
        },
        login,
        token,
    )
    if d_err(ads):
        print("  ", d_err(ads))
    else:
        got = ads["result"].get("Ads", [])
        print(f"  запрошено {len(ids)}, вернулось {len(got)}")
        for a in got[:6]:
            print("   ", json.dumps(a, ensure_ascii=False)[:300])
        missing = set(ids) - {a["Id"] for a in got}
        print(f"  не вернулись: {sorted(missing)[:10]}")

    print("\n--- 1c. Товарные фиды кабинета ---")
    feeds = direct_call(
        "feeds",
        {"FieldNames": ["Id", "Name", "BusinessType", "Source", "UpdateStatus", "FeedType"], "Page": {"Limit": 50}},
        login,
        token,
    )
    if d_err(feeds):
        print("  ", d_err(feeds))
    else:
        for f in feeds["result"].get("Feeds", []):
            print("   ", json.dumps(f, ensure_ascii=False)[:400])

    print("\n" + "=" * 78)
    print("2. Logs API: какие поля визитов доступны для сшивки с объявлением")
    print("=" * 78)
    token_m = os.environ["METRICA_TOKEN"]
    counter = os.environ["METRICA_COUNTER_ID"]
    headers = {"Authorization": f"OAuth {token_m}"}
    for field in (
        "ym:s:lastUTMContent",
        "ym:s:lastUTMCampaign",
        "ym:s:lastDirectClickBanner",
        "ym:s:lastDirectBannerGroup",
        "ym:s:lastDirectClickOrder",
        "ym:s:lastTrafficSource",
        "ym:s:lastAdvEngine",
    ):
        resp = requests.get(
            LOGS_URL.format(counter=counter) + "/evaluate",
            params={"date1": DATE1, "date2": DATE2, "fields": f"ym:s:visitID,{field}", "source": "visits"},
            headers=headers,
            timeout=120,
        )
        ok = resp.status_code == 200
        print(f"  {'OK ' if ok else resp.status_code} {field:<32} {'' if ok else resp.text[:110]}")
        time.sleep(0.3)


if __name__ == "__main__":
    main()
