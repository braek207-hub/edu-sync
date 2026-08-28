# Проба BJORN, раунд 11. В раунде 10 сшивка логов заработала (97% просмотров карточек нашли
# свой визит), но объявление в логах выглядит иначе, чем в stat API: там было `M-16448294678`,
# в логах — `1918366233890854573` и `17515027933`, а у 2/3 визитов вообще `0`.
# На этой связке держится требование «для платного воронка от показа объявления» — проверяю:
#   1. Совпадают ли id из логов с AdId из отчёта Директа (по множеству за август).
#   2. Как ложится покрытие: сколько рекламных визитов вообще имеют опознаваемое объявление,
#      в разрезе lastDirectClickBanner и lastUTMContent.
#   3. Заодно: все ли карточки товара лежат под /product/ (второй уровень адреса).
from __future__ import annotations

import hashlib
import json
import os
import time
from collections import Counter
from typing import Any

import requests

DIRECT_V5 = "https://api.direct.yandex.com/json/v5"
STAT = "https://api-metrika.yandex.net/stat/v1/data"
BASE = "https://api-metrika.yandex.net/management/v1/counter/{counter}"

DATE1 = os.environ.get("PROBE_DATE1", "2026-08-01")
DATE2 = os.environ.get("PROBE_DATE2", "2026-08-27")
LOG_DAY = os.environ.get("PROBE_LOG_DAY", "2026-08-20")
MAIN_LOGIN = os.environ.get("BJORN_MAIN_LOGIN", "audit-bjornlarsen")

TOKEN = os.environ["METRICA_TOKEN"]
COUNTER = os.environ["METRICA_COUNTER_ID"]
HEADERS = {"Authorization": f"OAuth {TOKEN}"}


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


def direct_report(login: str, token: str, fields: list[str]) -> tuple[int, str]:
    sig = hashlib.md5("|".join(fields + [login, DATE1, DATE2]).encode("utf-8")).hexdigest()[:10]
    body = {
        "params": {
            "SelectionCriteria": {"DateFrom": DATE1, "DateTo": DATE2},
            "FieldNames": fields,
            "ReportName": f"probe11_{login}_{sig}",
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


def logs_create(source: str, fields: str) -> int | None:
    resp = requests.post(
        BASE.format(counter=COUNTER) + "/logrequests",
        params={"date1": LOG_DAY, "date2": LOG_DAY, "fields": fields, "source": source},
        headers=HEADERS,
        timeout=120,
    )
    if resp.status_code != 200:
        print(f"    создание {source}: HTTP {resp.status_code} {resp.text[:250]}")
        return None
    return resp.json()["log_request"]["request_id"]


def logs_wait(request_id: int, tries: int = 40) -> dict | None:
    for _ in range(tries):
        resp = requests.get(BASE.format(counter=COUNTER) + f"/logrequest/{request_id}", headers=HEADERS, timeout=120)
        if resp.status_code != 200:
            return None
        info = resp.json()["log_request"]
        if info["status"] == "processed":
            return info
        if info["status"] in ("processing_failed", "canceled"):
            return None
        time.sleep(15)
    return None


def logs_text(info: dict, request_id: int) -> str:
    out = []
    for part in info.get("parts", []):
        resp = requests.get(
            BASE.format(counter=COUNTER) + f"/logrequest/{request_id}/part/{part['part_number']}/download",
            headers=HEADERS,
            timeout=600,
        )
        if resp.status_code == 200:
            out.append(resp.text)
    return "".join(out)


def logs_clean(request_id: int) -> None:
    requests.post(BASE.format(counter=COUNTER) + f"/logrequest/{request_id}/clean", headers=HEADERS, timeout=120)


def probe_sections() -> None:
    print("=" * 78)
    print(f"1. Разделы сайта по просмотрам (второй уровень адреса) · {DATE1}..{DATE2}")
    print("=" * 78)
    resp = requests.get(
        STAT,
        params={
            "ids": COUNTER, "date1": DATE1, "date2": DATE2, "accuracy": "full",
            "proposed_accuracy": "false", "lang": "ru", "limit": 20,
            "metrics": "ym:pv:pageviews,ym:pv:users", "dimensions": "ym:pv:URLPathLevel2",
        },
        headers=HEADERS,
        timeout=180,
    )
    body = resp.json() if resp.status_code == 200 else {}
    for d in body.get("data", []):
        print(f"    {d['metrics'][0]:>8.0f} {d['metrics'][1]:>7.0f}  {str(d['dimensions'][0].get('name'))[:66]}")


def main() -> None:
    probe_sections()

    print("\n" + "=" * 78)
    print(f"2. Множество AdId из отчёта Директа · {MAIN_LOGIN} · {DATE1}..{DATE2}")
    print("=" * 78)
    client = next((c for c in clients() if c["login"] == MAIN_LOGIN), None)
    if not client:
        print(f"  кабинет {MAIN_LOGIN} не найден")
        return
    status, text = direct_report(client["login"], client["token"], ["AdId", "CampaignName", "AdFormat", "Impressions", "Clicks"])
    if status != 200:
        print(f"  отчёт не отдался HTTP {status}: {text[:200]}")
        return
    ad_ids: set[str] = set()
    ad_meta: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        p = line.split("\t")
        if len(p) < 5 or not p[0].lstrip("-").isdigit():
            continue
        if int(p[3] or 0) <= 0:
            continue
        ad_ids.add(p[0])
        ad_meta[p[0]] = (p[1], p[2])
    lengths = Counter(len(a) for a in ad_ids)
    print(f"  объявлений с показами: {len(ad_ids)}, длина id: {sorted(lengths.items())}")
    print(f"  примеры: {sorted(ad_ids)[:6]}")

    print("\n" + "=" * 78)
    print(f"3. Сверка id объявления из логов с AdId · день {LOG_DAY}")
    print("=" * 78)
    visits_id = logs_create(
        "visits",
        "ym:s:visitID,ym:s:lastTrafficSource,ym:s:lastAdvEngine,ym:s:lastUTMSource,"
        "ym:s:lastUTMCampaign,ym:s:lastUTMContent,ym:s:lastDirectClickBanner,ym:s:lastDirectBannerGroup",
    )
    if not visits_id:
        return
    info = logs_wait(visits_id)
    if not info:
        logs_clean(visits_id)
        print("  лог не подготовился")
        return
    try:
        text = logs_text(info, visits_id)
        header = text.splitlines()[0].split("\t")
        print(f"  поля: {header}")
        idx = {name: i for i, name in enumerate(header)}

        ad_visits = 0
        banner_hit = banner_zero = banner_miss = 0
        content_hit = content_miss = content_empty = 0
        either_hit = 0
        miss_samples: Counter[str] = Counter()
        engines: Counter[str] = Counter()
        for line in text.splitlines()[1:]:
            p = line.split("\t")
            if len(p) < len(header):
                continue
            if p[idx["ym:s:lastTrafficSource"]] != "ad":
                continue
            ad_visits += 1
            engines[p[idx["ym:s:lastAdvEngine"]]] += 1
            banner = p[idx["ym:s:lastDirectClickBanner"]].strip()
            content = p[idx["ym:s:lastUTMContent"]].strip()
            b_ok = banner in ad_ids
            c_ok = content in ad_ids
            if banner in ("", "0"):
                banner_zero += 1
            elif b_ok:
                banner_hit += 1
            else:
                banner_miss += 1
                miss_samples[f"banner:{banner}"] += 1
            if not content:
                content_empty += 1
            elif c_ok:
                content_hit += 1
            else:
                content_miss += 1
                miss_samples[f"content:{content}"] += 1
            if b_ok or c_ok:
                either_hit += 1

        print(f"\n  рекламных визитов за день: {ad_visits}")
        print(f"  системы: {engines.most_common(6)}")
        print(f"  lastDirectClickBanner: совпало с AdId {banner_hit}, пусто/0 {banner_zero}, чужое {banner_miss}")
        print(f"  lastUTMContent:        совпало с AdId {content_hit}, пусто {content_empty}, чужое {content_miss}")
        print(f"  ХОТЯ БЫ ОДНО опознало объявление: {either_hit} ({either_hit / ad_visits * 100:.1f}%)" if ad_visits else "")
        print(f"  примеры несовпавших: {miss_samples.most_common(8)}")
    finally:
        logs_clean(visits_id)
        print("  запрос лога убран")


if __name__ == "__main__":
    main()
