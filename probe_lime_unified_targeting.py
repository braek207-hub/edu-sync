# -*- coding: utf-8 -*-
"""
probe_lime_unified_targeting.py — Ф0b Campaign Launcher: чем таргетируются
ЕПК-группы LIME (ретаргетинг/аудитории/корректировки) и какие FieldNames
поддерживают v501 adgroups/campaigns для UNIFIED-типов.

Read-only. Трюк: запрос с заведомо неверным значением в *FieldNames — Директ
в error_detail перечисляет допустимые значения перечисления.

ENV: DIRECT_TOKEN, DIRECT_CLIENT_LOGIN.
"""

import json
import os
from collections import Counter

import requests

BASE = "https://api.direct.yandex.com/json/v501"


def post(service, payload):
    headers = {
        "Authorization": f"Bearer {os.environ['DIRECT_TOKEN']}",
        "Client-Login": os.environ["DIRECT_CLIENT_LOGIN"],
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    resp = requests.post(f"{BASE}/{service}", data=data, headers=headers, timeout=120)
    resp.encoding = "utf-8"
    return resp.json()


def show_error_enum(title, service, payload):
    body = post(service, payload)
    err = body.get("error") or {}
    print(f"\n=== {title} ===")
    print(json.dumps(err.get("error_detail") or err or body.get("result", {}), ensure_ascii=False)[:1200])


def main():
    # Активные ЕПК → их группы.
    camps = post("campaigns", {"method": "get", "params": {
        "SelectionCriteria": {"Types": ["UNIFIED_CAMPAIGN"], "States": ["ON", "SUSPENDED"]},
        "FieldNames": ["Id", "Name"], "Page": {"Limit": 100}}})["result"].get("Campaigns", [])
    ids = [c["Id"] for c in camps]
    print(f"Активных ЕПК: {len(ids)}")

    groups = []
    for i in range(0, len(ids), 10):
        groups += post("adgroups", {"method": "get", "params": {
            "SelectionCriteria": {"CampaignIds": ids[i:i+10]},
            "FieldNames": ["Id", "CampaignId", "Type", "Name"], "Page": {"Limit": 10000}}})["result"].get("AdGroups", [])
    gids = [g["Id"] for g in groups]
    print(f"Групп: {len(gids)}")

    # 1. AudienceTargets по группам ЕПК.
    at_all = []
    at_err = None
    for i in range(0, min(len(gids), 200), 10):
        body = post("audiencetargets", {"method": "get", "params": {
            "SelectionCriteria": {"AdGroupIds": gids[i:i+10]},
            "FieldNames": ["Id", "AdGroupId", "RetargetingListId", "InterestId", "State"],
            "Page": {"Limit": 10000}}})
        if "error" in body:
            at_err = body["error"]
            break
        at_all += body["result"].get("AudienceTargets", [])
    print("\n=== AudienceTargets в ЕПК ===")
    if at_err:
        print("ОШИБКА:", json.dumps(at_err, ensure_ascii=False)[:600])
    else:
        print(f"всего: {len(at_all)}; с RetargetingListId: {sum(1 for a in at_all if a.get('RetargetingListId'))}; "
              f"с InterestId: {sum(1 for a in at_all if a.get('InterestId'))}")
        print("пример:", json.dumps(at_all[0], ensure_ascii=False) if at_all else "—")

    # 2. BidModifiers по ЕПК-кампаниям.
    bm_all = []
    bm_err = None
    for i in range(0, len(ids), 10):
        body = post("bidmodifiers", {"method": "get", "params": {
            "SelectionCriteria": {"CampaignIds": ids[i:i+10], "Levels": ["CAMPAIGN", "AD_GROUP"]},
            "FieldNames": ["Id", "CampaignId", "AdGroupId", "Type", "Level"],
            "Page": {"Limit": 10000}}})
        if "error" in body:
            bm_err = body["error"]
            break
        bm_all += body["result"].get("BidModifiers", [])
    print("\n=== BidModifiers в ЕПК ===")
    if bm_err:
        print("ОШИБКА:", json.dumps(bm_err, ensure_ascii=False)[:600])
    else:
        print(f"всего: {len(bm_all)}")
        print("по типам:", json.dumps(Counter(b["Type"] for b in bm_all), ensure_ascii=False))
        print("по уровням:", json.dumps(Counter(b["Level"] for b in bm_all), ensure_ascii=False))

    # 3. Полные перечни FieldNames через bogus-значение.
    show_error_enum("adgroups UnifiedAdGroupFieldNames (допустимые)", "adgroups", {
        "method": "get", "params": {
            "SelectionCriteria": {"Ids": gids[:1]},
            "FieldNames": ["Id"], "UnifiedAdGroupFieldNames": ["Bogus"]}})
    show_error_enum("adgroups FieldNames (допустимые)", "adgroups", {
        "method": "get", "params": {
            "SelectionCriteria": {"Ids": gids[:1]}, "FieldNames": ["Bogus"]}})
    show_error_enum("campaigns UnifiedCampaignFieldNames (допустимые)", "campaigns", {
        "method": "get", "params": {
            "SelectionCriteria": {"Ids": ids[:1]},
            "FieldNames": ["Id"], "UnifiedCampaignFieldNames": ["Bogus"]}})

    # 3b. Пакетные стратегии: сколько активных ЕПК сидят в пакетной.
    body = post("campaigns", {"method": "get", "params": {
        "SelectionCriteria": {"Ids": ids[:100]},
        "FieldNames": ["Id", "Name"],
        "UnifiedCampaignFieldNames": ["BiddingStrategy", "PackageBiddingStrategy"]}})
    print("\n=== Пакетные стратегии в активных ЕПК ===")
    if "error" in body:
        print("ОШИБКА (имя поля смотри в enum выше):", json.dumps(body["error"], ensure_ascii=False)[:600])
    else:
        rows = body["result"].get("Campaigns", [])
        packaged = [c for c in rows if (c.get("UnifiedCampaign") or {}).get("PackageBiddingStrategy")]
        print(f"в пакетной стратегии: {len(packaged)} из {len(rows)}")
        for c in packaged[:10]:
            print(" ", c["Id"], c["Name"], "->",
                  json.dumps(c["UnifiedCampaign"]["PackageBiddingStrategy"], ensure_ascii=False)[:200])

    # 4. Образец группы со всеми Unified-полями (если enum подскажет — здесь базовый набор).
    body = post("adgroups", {"method": "get", "params": {
        "SelectionCriteria": {"Ids": gids[:3]},
        "FieldNames": ["Id", "Name", "RegionIds", "NegativeKeywords", "TrackingParams", "Type"]}})
    print("\n=== Образец групп (базовые поля) ===")
    print(json.dumps(body.get("result", body.get("error")), ensure_ascii=False, indent=1)[:1500])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
