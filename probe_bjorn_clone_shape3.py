"""Последний слепок перед сборкой: объявление целиком и погодные корректировки с полями.

TrackingParams на уровне объявления get не принимает (поле есть только у группы и кампании),
поэтому убрано. Погодные корректировки читаются полями Temperature/Precipitation/CloudCover/
BidModifier/Enabled — проверяем, что в них лежит, чтобы перенести в клон.

Read-only.
"""

from __future__ import annotations

import json
import os

import requests

API = "https://api.direct.yandex.com/json/v5/"
CAMPAIGN = 714000003
SAMPLE_GROUP = 5796196259


def call(service: str, params: dict, login: str, token: str, method: str = "get") -> dict:
    body = json.dumps({"method": method, "params": params}, ensure_ascii=False).encode("utf-8")
    resp = requests.post(API + service, data=body, headers={
        "Authorization": f"Bearer {token}",
        "Client-Login": login,
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
    }, timeout=120)
    try:
        return resp.json()
    except Exception:
        return {"error": {"error_string": f"HTTP {resp.status_code}", "error_detail": resp.text[:300]}}


def clients() -> list[tuple[str, str]]:
    default_token = os.environ.get("DIRECT_TOKEN", "").strip()
    raw = os.environ.get("DIRECT_CLIENTS_JSON", "").strip()
    out: list[tuple[str, str]] = []
    if raw:
        for item in json.loads(raw):
            if isinstance(item, dict):
                login = str(item.get("login") or item.get("client_login") or "").strip()
                token = str(item.get("token") or "").strip() or default_token
                if login:
                    out.append((login, token))
            elif isinstance(item, str):
                out.append((item.strip(), default_token))
    return out


def dump(login: str, token: str) -> None:
    print(f"### ОБЪЯВЛЕНИЯ ГРУППЫ {SAMPLE_GROUP}")
    a = call("ads", {
        "SelectionCriteria": {"AdGroupIds": [SAMPLE_GROUP]},
        "FieldNames": ["Id", "AdGroupId", "Type", "Subtype", "State", "Status"],
        "TextAdFieldNames": ["Title", "Title2", "Text", "Href", "Mobile", "DisplayDomain",
                             "DisplayUrlPath", "AdImageHash", "LogoExtensionHash",
                             "SitelinkSetId", "VCardId", "AdExtensions", "VideoExtension",
                             "TurboPageId", "BusinessId", "PreferVCardOverBusiness",
                             "ButtonExtension"],
        "Page": {"Limit": 10},
    }, login, token)
    print(json.dumps(a.get("result") or a, ensure_ascii=False, indent=1)[:3000])

    print(f"\n### ПОГОДНЫЕ И ДЕМОГРАФИЧЕСКИЕ КОРРЕКТИРОВКИ ГРУППЫ {SAMPLE_GROUP}")
    b = call("bidmodifiers", {
        "SelectionCriteria": {"AdGroupIds": [SAMPLE_GROUP], "Levels": ["AD_GROUP"]},
        "FieldNames": ["Id", "AdGroupId", "Level", "Type"],
        "WeatherAdjustmentFieldNames": ["Temperature", "Precipitation", "CloudCover",
                                        "BidModifier", "Enabled"],
        "DemographicsAdjustmentFieldNames": ["Gender", "Age", "BidModifier"],
        "Page": {"Limit": 50},
    }, login, token)
    print(json.dumps(b.get("result") or b, ensure_ascii=False, indent=1)[:2500])

    print("\n### КОРРЕКТИРОВКИ УРОВНЯ КАМПАНИИ")
    b2 = call("bidmodifiers", {
        "SelectionCriteria": {"CampaignIds": [CAMPAIGN], "Levels": ["CAMPAIGN"]},
        "FieldNames": ["Id", "CampaignId", "Level", "Type"],
        "MobileAdjustmentFieldNames": ["BidModifier", "OsType"],
        "DesktopAdjustmentFieldNames": ["BidModifier"],
        "TabletAdjustmentFieldNames": ["BidModifier", "OsType"],
        "SmartTVAdjustmentFieldNames": ["BidModifier"],
        "DemographicsAdjustmentFieldNames": ["Gender", "Age", "BidModifier"],
        "RetargetingAdjustmentFieldNames": ["RetargetingConditionId", "BidModifier"],
        "IncomeGradeAdjustmentFieldNames": ["Grade", "BidModifier"],
        "Page": {"Limit": 50},
    }, login, token)
    print(json.dumps(b2.get("result") or b2, ensure_ascii=False, indent=1)[:2500])


def main() -> None:
    for login, token in clients():
        if token:
            print(f"аккаунт {login}\n")
            dump(login, token)
            return


if __name__ == "__main__":
    main()
