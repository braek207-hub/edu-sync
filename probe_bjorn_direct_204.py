"""Почему Reports API отвечает 204 кабинету BJORN с 11.10.2026. Только чтение.

Запуск: workflow probe-bjorn-products-ads, script=probe_bjorn_direct_204.py.
Проверяем: доступ к кабинету (Campaigns.get), тот же отчёт под тем же именем, что у синка,
тот же отчёт под новым именем (кэш отчёта по ReportName), один день, вчерашнее окно.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid

import requests

from sync.bjorn.sync_direct import REPORTS_URL, direct_headers, load_client_logins

FIELDS = ["Date", "CampaignId", "CampaignName", "Impressions", "Clicks", "Cost"]


def sync_name(login: str, d1: str, d2: str) -> str:
    signature = hashlib.md5("|".join(FIELDS + [login, d1, d2]).encode("utf-8")).hexdigest()[:10]
    return f"MarketingSupabase_{login}_{d1}_{d2}_{signature}"


def report(label: str, login: str, token: str, d1: str, d2: str, name: str) -> None:
    body = {
        "params": {
            "SelectionCriteria": {"DateFrom": d1, "DateTo": d2},
            "FieldNames": FIELDS,
            "ReportName": name,
            "ReportType": "CUSTOM_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES",
            "IncludeDiscount": "NO",
        }
    }
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    for attempt in range(12):
        r = requests.post(
            REPORTS_URL, data=payload, headers=direct_headers(token, login, include_column_header=True), timeout=120
        )
        r.encoding = "utf-8"
        keep = {k: r.headers.get(k) for k in ("RequestId", "retryIn", "reportsInQueue", "Units", "Content-Length")}
        lines = r.text.splitlines()
        print(f"[{label}] попытка {attempt}: HTTP {r.status_code} заголовки={keep} строк={len(lines)}")
        if r.status_code == 200:
            for line in lines[:3]:
                print(f"[{label}]   {line[:160]}")
            return
        if r.status_code in (201, 202):
            time.sleep(int(r.headers.get("retryIn", 10)))
            continue
        print(f"[{label}]   тело: {r.text[:300]!r}")
        if r.status_code != 204:
            return
        time.sleep(10)


def main() -> None:
    for client in load_client_logins():
        login, token = client["login"], client["token"]
        print(f"== кабинет {login}")
        resp = requests.post(
            "https://api.direct.yandex.com/json/v5/campaigns",
            data=json.dumps({"method": "get", "params": {
                "SelectionCriteria": {}, "FieldNames": ["Id", "State", "Status"],
            }}).encode("utf-8"),
            headers={"Authorization": f"Bearer {token}", "Client-Login": login,
                     "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8"},
            timeout=120,
        )
        data = resp.json()
        if "error" in data:
            print(f"[campaigns] ошибка: {data['error']}")
        else:
            states: dict[str, int] = {}
            for c in data.get("result", {}).get("Campaigns", []):
                states[c["State"]] = states.get(c["State"], 0) + 1
            print(f"[campaigns] HTTP {resp.status_code}, состояния: {states}")

        fresh = uuid.uuid4().hex[:8]
        report("A имя синка 04-10", login, token, "2026-10-04", "2026-10-10", sync_name(login, "2026-10-04", "2026-10-10"))
        report("B новое имя 04-10", login, token, "2026-10-04", "2026-10-10", f"Probe204_{fresh}_a")
        report("C один день 10.10", login, token, "2026-10-10", "2026-10-10", f"Probe204_{fresh}_b")
        report("D один день 04.10", login, token, "2026-10-04", "2026-10-04", f"Probe204_{fresh}_c")
        report("E имя синка 03-09", login, token, "2026-10-03", "2026-10-09", sync_name(login, "2026-10-03", "2026-10-09"))


if __name__ == "__main__":
    main()
