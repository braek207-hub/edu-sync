"""Проба: чем представлены смарт- и динамические объявления Bjorn в ads.get.

Вопрос один: есть ли у SHOPPING_AD и LISTING_AD хоть какой-то блок с картинкой или
креативом. В витрине у всех тридцати пусто, и надо понять — так устроен Директ или мы не
запросили нужный блок. Печатаются ИМЕНА полей и типы, не содержимое.

SelectionCriteria у ads.get обязателен (Ids / AdGroupIds / CampaignIds) — берём id прямо
из витрины bjorn_ad_entities.
"""

from __future__ import annotations

import json
from collections import defaultdict

import requests

from sync.bjorn.common import SupabaseRest
from sync.bjorn.sync_direct import load_client_logins

API = "https://api.direct.yandex.com/json/v5/"

ALL_BLOCKS = {
    "TextAdFieldNames": ["Title", "Title2", "Text", "Href", "AdImageHash"],
    "TextImageAdFieldNames": ["AdImageHash", "Href"],
    "TextAdBuilderAdFieldNames": ["Creative", "Href"],
    "DynamicTextAdFieldNames": ["Text", "AdImageHash"],
    "SmartAdBuilderAdFieldNames": ["Creative"],
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
    supabase = SupabaseRest()
    entities = supabase.request(
        "GET", "bjorn_ad_entities", params={"select": "ad_id,ad_type"}
    ).json()
    wanted = [e for e in entities if e["ad_type"] in {"SHOPPING_AD", "LISTING_AD", "TEXT_AD"}]
    print(f"объявлений в витрине {len(entities)}, спрашиваем {len(wanted)}")

    client = load_client_logins()[0]
    login, token = client["login"], client["token"]

    params = {
        "SelectionCriteria": {"Ids": [int(e["ad_id"]) for e in wanted]},
        "FieldNames": ["Id", "Type", "Subtype", "CampaignId"],
        "Page": {"Limit": 2000},
        **ALL_BLOCKS,
    }
    body = call("ads", params, login, token)
    if body.get("error"):
        print(f"ads.get ошибка: {body['error']}")
        return

    ads = body["result"].get("Ads", [])
    by_type: dict[str, list[dict]] = defaultdict(list)
    for ad in ads:
        by_type[str(ad.get("Type"))].append(ad)

    for ad_type, items in sorted(by_type.items()):
        keys: dict[str, int] = defaultdict(int)
        for ad in items:
            for key in ad:
                keys[key] += 1
        print(f"{ad_type}: {len(items)} → {dict(sorted(keys.items()))}")
        for ad in items[:2]:
            for key, value in ad.items():
                if isinstance(value, dict):
                    print(f"  {ad_type}.{key} = {json.dumps(value, ensure_ascii=False)[:300]}")


if __name__ == "__main__":
    main()
