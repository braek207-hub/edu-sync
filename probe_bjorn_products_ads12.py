# Проба BJORN, раунд 12. В раунде 11 25% рекламных визитов сослались на id объявлений,
# которых нет в кабинете audit-bjornlarsen (17515027933, 17619808383, 17791264885 и др.).
# Если это второй кабинет — решение «второй кабинет не трогаем» стоит нам четверти
# привязки объявление↔покупка, и это надо знать до реализации, а не после.
# Проверяю принадлежность конкретных id: отчёт по каждому кабинету + ads.get по Ids.
from __future__ import annotations

import hashlib
import json
import os
import time

import requests

DIRECT_V5 = "https://api.direct.yandex.com/json/v5"
DATE1 = os.environ.get("PROBE_DATE1", "2026-08-01")
DATE2 = os.environ.get("PROBE_DATE2", "2026-08-27")

UNKNOWN = [
    "17515027933", "17515027934", "17619808383", "17791264885",
    "17791264863", "17791264857", "17791264871", "17791264893",
]


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
        return {"error": {"error_code": resp.status_code, "error_detail": resp.text[:300]}}


def direct_report(login: str, token: str, fields: list[str]) -> tuple[int, str]:
    sig = hashlib.md5("|".join(fields + [login, DATE1, DATE2, "r12"]).encode("utf-8")).hexdigest()[:10]
    body = {
        "params": {
            "SelectionCriteria": {"DateFrom": DATE1, "DateTo": DATE2},
            "FieldNames": fields,
            "ReportName": f"probe12_{login}_{sig}",
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
    unknown = set(UNKNOWN)
    for c in clients():
        login, token = c["login"], c["token"]
        print("=" * 78)
        print(f"кабинет {login}")
        print("=" * 78)

        status, text = direct_report(login, token, ["AdId", "CampaignName", "AdFormat", "Impressions", "Clicks", "Cost"])
        if status != 200:
            print(f"  отчёт: HTTP {status} {text[:200]}")
        else:
            ids = set()
            spend = 0.0
            for line in text.splitlines():
                p = line.split("\t")
                if len(p) < 6 or not p[0].lstrip("-").isdigit():
                    continue
                if int(p[3] or 0) > 0:
                    ids.add(p[0])
                spend += float(p[5] or 0)
            print(f"  объявлений с показами за период: {len(ids)}, расход {spend:.0f} ₽")
            print(f"  из спорных id найдено здесь: {sorted(unknown & ids)}")

        ads = direct_call(
            "ads",
            {
                "SelectionCriteria": {"Ids": [int(i) for i in UNKNOWN]},
                "FieldNames": ["Id", "CampaignId", "AdGroupId", "Type", "Subtype", "State", "Status"],
                "Page": {"Limit": 50},
            },
            login,
            token,
        )
        if ads.get("error"):
            e = ads["error"]
            print(f"  ads.get: ошибка {e.get('error_code')} {str(e.get('error_detail'))[:180]}")
        else:
            got = ads["result"].get("Ads", [])
            print(f"  ads.get по спорным Ids вернул: {len(got)}")
            for a in got[:8]:
                print("   ", json.dumps(a, ensure_ascii=False)[:220])


if __name__ == "__main__":
    main()
