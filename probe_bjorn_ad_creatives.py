"""Проба: чем вообще представлены смарт- и динамические объявления Bjorn в ads.get.

Вопрос один: есть ли у SHOPPING_AD и LISTING_AD хоть какой-то блок с картинкой или
креативом. В витрине у всех тридцати пусто, и надо понять — так устроен Директ или
мы не запросили нужный блок. Печатаются только ИМЕНА полей и типы, не содержимое.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict

import requests

from sync.bjorn.sync_direct import load_client_logins

API = "https://api.direct.yandex.com/json/v5/"

ALL_BLOCKS = {
    "TextAdFieldNames": ["Title", "Title2", "Text", "Href", "AdImageHash"],
    "TextImageAdFieldNames": ["AdImageHash", "Href"],
    "TextAdBuilderAdFieldNames": ["Creative", "Href"],
    "DynamicTextAdFieldNames": ["Text", "AdImageHash"],
    "SmartAdBuilderAdFieldNames": ["Creative"],
    "MobileAppAdBuilderAdFieldNames": ["Creative"],
    "CpmBannerAdBuilderAdFieldNames": ["Creative"],
}


def call(service: str, params: dict, login: str, token: str) -> dict:
    body = json.dumps({"method": "get", "params": params}, ensure_ascii=False).encode("utf-8")
    resp = requests.post(
        API + service,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Client-Login": login,
            "Accept-Language": "ru",
            "Content-Type": "application/json; charset=utf-8",
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    clients = load_client_logins()
    client = clients[0]
    login, token = client["login"], client["token"]

    for blocks in ({}, ALL_BLOCKS):
        label = "без блоков" if not blocks else "со всеми блоками"
        params = {
            "SelectionCriteria": {"States": ["ON"]},
            "FieldNames": ["Id", "Type", "Subtype", "CampaignId"],
            "Page": {"Limit": 2000},
        }
        params.update(blocks)
        body = call("ads", params, login, token)
        if body.get("error"):
            print(f"[{label}] ошибка: {body['error']}")
            continue
        ads = body["result"].get("Ads", [])
        by_type: dict[str, list[dict]] = defaultdict(list)
        for ad in ads:
            by_type[str(ad.get("Type"))].append(ad)
        print(f"[{label}] объявлений {len(ads)}")
        for ad_type, items in sorted(by_type.items()):
            keys: dict[str, int] = defaultdict(int)
            for ad in items:
                for key in ad:
                    keys[key] += 1
            print(f"  {ad_type}: {len(items)} → {dict(sorted(keys.items()))}")
            sample = items[0]
            for key, value in sample.items():
                if isinstance(value, dict):
                    print(f"    {ad_type}.{key} = {sorted(value.keys())}")

    # Есть ли вообще креативы в кабинете и какого они типа.
    body = call("creatives", {"SelectionCriteria": {}, "FieldNames": ["Id", "Type"]}, login, token)
    if body.get("error"):
        print(f"creatives.get ошибка: {body['error']}")
    else:
        creatives = body["result"].get("Creatives", [])
        counts: dict[str, int] = defaultdict(int)
        for c in creatives:
            counts[str(c.get("Type"))] += 1
        print(f"креативов в кабинете {len(creatives)}: {dict(counts)}")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    main()
