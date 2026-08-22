# -*- coding: utf-8 -*-
"""
probe_lime_unified_structure.py — Ф0 фичи Campaign Launcher (спека
panda-bi docs/superpowers/specs/2026-08-22-lime-campaign-launcher-design.md).

Читающая проба кабинета LIME через v501: какие типы групп и объявлений
ФАКТИЧЕСКИ живут в ЕПК-кампаниях. Снимает нестыковку справки: RESPONSIVE_AD
не упомянут среди типов объявлений UNIFIED_AD_GROUP.

Ничего не пишет. ENV: DIRECT_TOKEN, DIRECT_CLIENT_LOGIN.
"""

import json
import os
from collections import Counter
from typing import Any, Dict, List

import requests

BASE = "https://api.direct.yandex.com/json/v501"


def _post(service: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {os.environ['DIRECT_TOKEN']}",
        "Client-Login": os.environ["DIRECT_CLIENT_LOGIN"],
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    resp = requests.post(f"{BASE}/{service}", data=data, headers=headers, timeout=120)
    resp.encoding = "utf-8"
    body = resp.json()
    if "error" in body:
        raise RuntimeError(f"{service}: {json.dumps(body['error'], ensure_ascii=False)}")
    return body["result"]


def main() -> int:
    # 1. Все кампании, разрез по типам.
    campaigns: List[Dict[str, Any]] = []
    offset = 0
    while True:
        res = _post(
            "campaigns",
            {
                "method": "get",
                "params": {
                    "SelectionCriteria": {},
                    "FieldNames": ["Id", "Name", "Type", "State", "Status"],
                    "Page": {"Limit": 1000, "Offset": offset},
                },
            },
        )
        campaigns.extend(res.get("Campaigns") or [])
        if res.get("LimitedBy") is None:
            break
        offset = res["LimitedBy"]

    print("=== Кампании по типам (все) ===")
    print(json.dumps(Counter(c["Type"] for c in campaigns), ensure_ascii=False, indent=2))

    unified = [c for c in campaigns if c["Type"] == "UNIFIED_CAMPAIGN"]
    active_unified = [c for c in unified if c["State"] in ("ON", "SUSPENDED")]
    print(f"\nЕПК всего: {len(unified)}, активных/приостановленных: {len(active_unified)}")
    for c in active_unified[:20]:
        print(f"  {c['Id']}  {c['State']:9}  {c['Name']}")

    if not unified:
        print("VERDICT: NO_UNIFIED_CAMPAIGNS")
        return 0

    ids = [c["Id"] for c in unified]

    # 2. Группы ЕПК: типы.
    groups: List[Dict[str, Any]] = []
    for i in range(0, len(ids), 10):
        res = _post(
            "adgroups",
            {
                "method": "get",
                "params": {
                    "SelectionCriteria": {"CampaignIds": ids[i : i + 10]},
                    "FieldNames": ["Id", "CampaignId", "Type", "Name"],
                    "Page": {"Limit": 10000},
                },
            },
        )
        groups.extend(res.get("AdGroups") or [])

    print("\n=== Группы в ЕПК по типам ===")
    print(json.dumps(Counter(g["Type"] for g in groups), ensure_ascii=False, indent=2))

    # 3. Объявления ЕПК: типы + подтипы.
    ads: List[Dict[str, Any]] = []
    gids = [g["Id"] for g in groups]
    for i in range(0, len(gids), 10):
        res = _post(
            "ads",
            {
                "method": "get",
                "params": {
                    "SelectionCriteria": {"AdGroupIds": gids[i : i + 10]},
                    "FieldNames": ["Id", "AdGroupId", "Type", "Subtype", "State", "Status"],
                    "Page": {"Limit": 10000},
                },
            },
        )
        ads.extend(res.get("Ads") or [])

    print("\n=== Объявления в ЕПК по типам ===")
    print(json.dumps(Counter(a["Type"] for a in ads), ensure_ascii=False, indent=2))
    print("\n=== Подтипы ===")
    print(json.dumps(Counter(str(a.get("Subtype")) for a in ads), ensure_ascii=False, indent=2))

    # 4. Образец структуры одного объявления каждого типа (для клона).
    print("\n=== Образцы объявлений (по одному на тип) ===")
    seen = set()
    for a in ads:
        if a["Type"] in seen:
            continue
        seen.add(a["Type"])
        field_map = {
            "RESPONSIVE_AD": ["ResponsiveAdFieldNames", ["Titles", "Texts", "AdImageHashes", "Href", "VideoExtensionIds"]],
            "TEXT_AD": ["TextAdFieldNames", ["Title", "Title2", "Text", "Href", "AdImageHash"]],
            "SHOPPING_AD": ["ShoppingAdFieldNames", ["Name"]],
        }
        extra = field_map.get(a["Type"])
        params: Dict[str, Any] = {
            "SelectionCriteria": {"Ids": [a["Id"]]},
            "FieldNames": ["Id", "Type", "State", "Status"],
        }
        if extra:
            params[extra[0]] = extra[1]
        try:
            res = _post("ads", {"method": "get", "params": params})
            sample = (res.get("Ads") or [{}])[0]
            print(f"\n-- {a['Type']} --")
            print(json.dumps(sample, ensure_ascii=False, indent=2)[:1500])
        except RuntimeError as exc:
            print(f"\n-- {a['Type']} -- FieldNames-проба не прошла: {exc}")

    responsive_in_unified = sum(
        1 for a in ads if a["Type"] == "RESPONSIVE_AD"
    )
    print(
        f"\nVERDICT: RESPONSIVE_AD в ЕПК-группах: {responsive_in_unified} шт. "
        + ("— справка неполна, комбинаторные живут в ЕПК" if responsive_in_unified else "— в кабинете их нет, тип объявлений ЕПК другой (см. счётчики выше)")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
