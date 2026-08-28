"""Статистика объявлений Bjorn: расход из Директа + поведение и покупки из Метрики.

Три источника, все проверены пробами 5-7 и 12:

1. Отчёт Директа CUSTOM_REPORT (Date, CampaignId, CampaignName, AdGroupId, AdId, AdFormat,
   Impressions, Clicks, Cost) с IncludeVAT: YES — НДС УЖЕ ВНУТРИ, умножать на 1.2 нельзя.
2. stat API Метрики, срез день x объявление (ym:s:lastsignDirectBanner) — визиты, отказы,
   глубина, корзины и покупки на каждое объявление.
3. Справочник объявлений и картинок: ads.get -> adimages.get / creatives.get.

Что ломает синк, если этого не знать:

- SelectionCriteria.CampaignIds принимает не больше ДЕСЯТИ кампаний (на этом падала проба 5);
- параметра ImageAdFieldNames не существует вовсе — тоже проба 5;
- в справочник идут только объявления с показами за период: проба 6 без этого фильтра
  притащила 755 картинок архива против 48 реально работавших объявлений;
- у смарт-баннеров (SHOPPING_AD) файла картинки нет и не будет: креатив собирается из
  товарного фида на лету. Пустой preview_url там — норма, а не потеря;
- ym:s:lastsignDirectBanner отдаёт «M-16448294678», а Директ — голый id. Срезаем «M-».
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import time
from collections import defaultdict
from typing import Any

import requests

from .common import (
    SupabaseRest,
    add_date_range_args,
    clean_text,
    env_optional,
    env_required,
    money,
    parse_int,
    parse_optional_float,
    resolve_date_range,
)
from .sync_direct import fetch_report, load_client_logins

DIRECT_API = "https://api.direct.yandex.com/json/v5/{service}"
STAT_URL = "https://api-metrika.yandex.net/stat/v1/data"

DAILY_TABLE = "bjorn_ad_daily"
DAILY_ON_CONFLICT = "date,ad_id"
ENTITIES_TABLE = "bjorn_ad_entities"
ENTITIES_ON_CONFLICT = "ad_id"

REPORT_FIELDS = [
    "Date",
    "CampaignId",
    "CampaignName",
    "AdGroupId",
    "AdId",
    "AdFormat",
    "Impressions",
    "Clicks",
    "Cost",
]

# Лимит API — десять кампаний на запрос. Не поднимать: одиннадцатая роняет весь вызов.
CAMPAIGNS_PER_CALL = 10

AD_FIELD_BLOCKS = {
    "TextAdFieldNames": ["Title", "Title2", "Text", "Href", "AdImageHash"],
    "TextImageAdFieldNames": ["AdImageHash", "Href"],
    "TextAdBuilderAdFieldNames": ["Creative", "Href"],
    "DynamicTextAdFieldNames": ["Text", "AdImageHash"],
    "SmartAdBuilderAdFieldNames": ["Creative"],
}

METRICS = [
    "ym:s:visits",
    "ym:s:users",
    "ym:s:bounceRate",
    "ym:s:pageDepth",
    "ym:s:productBasketsUniq",
    "ym:s:productPurchasedQuantity",
    "ym:s:productPurchasedPrice",
]


def strip_banner(value: Any) -> str:
    text = clean_text(value)
    if text in {"None", "null", "--", "(not set)"}:
        return ""
    return text[2:] if text.startswith("M-") else text


def direct_call(service: str, params: dict[str, Any], login: str, token: str) -> dict[str, Any]:
    body = json.dumps({"method": "get", "params": params}, ensure_ascii=False).encode("utf-8")
    for attempt in range(5):
        resp = requests.post(
            DIRECT_API.format(service=service),
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Client-Login": login,
                "Accept-Language": "ru",
                "Content-Type": "application/json; charset=utf-8",
            },
            timeout=180,
        )
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in {429, 500, 502, 503, 504} and attempt < 4:
            time.sleep(min(2**attempt * 5, 60))
            continue
        raise RuntimeError(f"Direct {service}.get: {resp.status_code} {resp.text[:300]}")
    raise RuntimeError(f"Direct {service}.get: попытки исчерпаны")


def fetch_daily(login: str, token: str, date_from: str, date_to: str) -> dict[str, dict[str, Any]]:
    """Строки отчёта Директа в грани день x объявление. Ключ — (день, id объявления)."""
    text = fetch_report(login, date_from, date_to, REPORT_FIELDS, token=token)
    rows: dict[str, dict[str, Any]] = {}
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    for item in reader:
        ad_id = clean_text(item.get("AdId"))
        day = clean_text(item.get("Date"))
        if not ad_id or not day:
            continue
        key = f"{day}|{ad_id}"
        row = rows.setdefault(
            key,
            {
                "date": day,
                "ad_id": ad_id,
                "campaign_id": clean_text(item.get("CampaignId")),
                "campaign_name": clean_text(item.get("CampaignName")),
                "adgroup_id": clean_text(item.get("AdGroupId")),
                "ad_format": clean_text(item.get("AdFormat")),
                "impressions": 0,
                "clicks": 0,
                "cost_vat": 0.0,
            },
        )
        row["impressions"] += parse_int(item.get("Impressions"))
        row["clicks"] += parse_int(item.get("Clicks"))
        # Расход уже с НДС: отчёт запрошен с IncludeVAT: YES.
        row["cost_vat"] += money(item.get("Cost"))
    return rows


def fetch_metrica(date_from: str, date_to: str) -> dict[str, dict[str, Any]]:
    """Поведение и покупки на объявление. Модель lastsign — та же, что у товарной витрины."""
    token = env_required("METRICA_TOKEN")
    counter = env_required("METRICA_COUNTER_ID")
    out: dict[str, dict[str, Any]] = {}
    limit = 100000
    offset = 1
    while True:
        resp = requests.get(
            STAT_URL,
            params={
                "ids": counter,
                "metrics": ",".join(METRICS),
                "dimensions": "ym:s:date,ym:s:lastsignDirectBanner",
                "date1": date_from,
                "date2": date_to,
                "accuracy": "full",
                "attribution": "lastsign",
                "limit": limit,
                "offset": offset,
            },
            headers={"Authorization": f"OAuth {token}"},
            timeout=180,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Metrica stat: {resp.status_code} {resp.text[:300]}")
        data = resp.json().get("data", [])
        for item in data:
            dims = [d.get("name") for d in item.get("dimensions", [])]
            values = item.get("metrics", [])
            if len(dims) < 2 or len(values) < len(METRICS):
                continue
            ad_id = strip_banner(dims[1])
            if not ad_id:
                continue
            out[f"{clean_text(dims[0])}|{ad_id}"] = {
                "visits": parse_int(values[0]),
                "users": parse_int(values[1]),
                "bounce_rate": parse_optional_float(values[2]),
                "page_depth": parse_optional_float(values[3]),
                "baskets_uniq": parse_int(values[4]),
                "purchased": parse_int(values[5]),
                "revenue": round(money(values[6]), 2),
            }
        if len(data) < limit:
            break
        offset += limit
    return out


def fetch_entities(login: str, token: str, ad_ids: set[str]) -> list[dict[str, Any]]:
    """Справочник ТОЛЬКО по объявлениям с показами за период.

    Без этого фильтра приезжает архив мёртвых креативов за все годы (проба 6: 755 картинок
    против 48 работавших объявлений).
    """
    if not ad_ids:
        return []

    campaigns = direct_call(
        "campaigns", {"SelectionCriteria": {}, "FieldNames": ["Id"]}, login, token
    )
    if campaigns.get("error"):
        raise RuntimeError(f"campaigns.get: {campaigns['error']}")
    campaign_ids = [str(c["Id"]) for c in campaigns["result"].get("Campaigns", [])]

    ads: list[dict[str, Any]] = []
    for start in range(0, len(campaign_ids), CAMPAIGNS_PER_CALL):
        body = direct_call(
            "ads",
            {
                "SelectionCriteria": {
                    "CampaignIds": campaign_ids[start : start + CAMPAIGNS_PER_CALL]
                },
                "FieldNames": ["Id", "CampaignId", "AdGroupId", "Type", "Subtype", "State"],
                "Page": {"Limit": 10000},
                **AD_FIELD_BLOCKS,
            },
            login,
            token,
        )
        if body.get("error"):
            raise RuntimeError(f"ads.get: {body['error']}")
        ads.extend(body["result"].get("Ads", []))
        time.sleep(0.2)

    live = [ad for ad in ads if str(ad.get("Id")) in ad_ids]
    print(f"[bjorn/ads] объявлений в кабинете {len(ads)}, с показами за период {len(live)}")

    image_hashes: set[str] = set()
    creative_ids: set[str] = set()
    for ad in live:
        for block in ("TextAd", "TextImageAd", "DynamicTextAd"):
            image_hash = (ad.get(block) or {}).get("AdImageHash")
            if image_hash:
                image_hashes.add(image_hash)
        for block in ("TextAdBuilderAd", "SmartAdBuilderAd"):
            creative = (ad.get(block) or {}).get("Creative") or {}
            if creative.get("CreativeId"):
                creative_ids.add(str(creative["CreativeId"]))

    images: dict[str, dict[str, Any]] = {}
    if image_hashes:
        body = direct_call(
            "adimages",
            {
                "SelectionCriteria": {"AdImageHashes": sorted(image_hashes)},
                "FieldNames": ["AdImageHash", "Name", "PreviewUrl", "OriginalUrl"],
            },
            login,
            token,
        )
        if body.get("error"):
            raise RuntimeError(f"adimages.get: {body['error']}")
        images = {i["AdImageHash"]: i for i in body["result"].get("AdImages", [])}

    creatives: dict[str, dict[str, Any]] = {}
    if creative_ids:
        body = direct_call(
            "creatives",
            {
                "SelectionCriteria": {"Ids": [int(c) for c in creative_ids]},
                "FieldNames": ["Id", "PreviewUrl"],
            },
            login,
            token,
        )
        if body.get("error"):
            raise RuntimeError(f"creatives.get: {body['error']}")
        creatives = {str(c["Id"]): c for c in body["result"].get("Creatives", [])}

    rows: list[dict[str, Any]] = []
    for ad in live:
        text_ad = ad.get("TextAd") or {}
        image_ad = ad.get("TextImageAd") or {}
        dynamic_ad = ad.get("DynamicTextAd") or {}
        builder = (ad.get("TextAdBuilderAd") or {}).get("Creative") or {}
        smart = (ad.get("SmartAdBuilderAd") or {}).get("Creative") or {}

        image_hash = (
            text_ad.get("AdImageHash") or image_ad.get("AdImageHash") or dynamic_ad.get("AdImageHash") or ""
        )
        image = images.get(image_hash, {})
        creative_id = str(builder.get("CreativeId") or smart.get("CreativeId") or "")
        creative = creatives.get(creative_id, {})

        rows.append(
            {
                "ad_id": str(ad["Id"]),
                "campaign_id": str(ad.get("CampaignId") or ""),
                "adgroup_id": str(ad.get("AdGroupId") or ""),
                "ad_type": clean_text(ad.get("Type")),
                "ad_format": clean_text(ad.get("Subtype")),
                "title": clean_text(text_ad.get("Title")),
                "title2": clean_text(text_ad.get("Title2")),
                "body": clean_text(text_ad.get("Text") or dynamic_ad.get("Text")),
                "href": clean_text(text_ad.get("Href") or image_ad.get("Href") or builder.get("Href")),
                "image_hash": image_hash,
                "image_name": clean_text(image.get("Name")),
                "preview_url": clean_text(image.get("PreviewUrl")),
                "original_url": clean_text(image.get("OriginalUrl")),
                "creative_id": creative_id,
                "creative_preview_url": clean_text(creative.get("PreviewUrl")),
            }
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Bjorn ad statistics to Supabase.")
    add_date_range_args(parser)
    args = parser.parse_args()
    date_from, date_to = resolve_date_range(args)

    # Только первый кабинет: второй за август не открутил ни рубля (проба 12), и трогать его
    # решено не было. Явный логин через DIRECT_ADS_LOGIN, если порядок в списке изменится.
    clients = load_client_logins()
    wanted = env_optional("DIRECT_ADS_LOGIN")
    client = next((c for c in clients if c["login"] == wanted), clients[0])
    print(f"[bjorn/ads] кабинет {client['login']}")

    daily = fetch_daily(client["login"], client["token"], date_from, date_to)
    metrica = fetch_metrica(date_from, date_to)

    for key, extra in metrica.items():
        row = daily.get(key)
        # Объявление есть в Метрике, но не в отчёте Директа — это чужой кабинет (проба 12:
        # 45 таких id не нашлись ни в одном из двух). Своей строки в витрине оно не получает.
        if row is not None:
            row.update(extra)

    rows = list(daily.values())
    entities = fetch_entities(client["login"], client["token"], {r["ad_id"] for r in rows})

    supabase = SupabaseRest()
    supabase.delete_date_range(DAILY_TABLE, date_from, date_to)
    supabase.upsert(DAILY_TABLE, rows, DAILY_ON_CONFLICT)
    supabase.upsert(ENTITIES_TABLE, entities, ENTITIES_ON_CONFLICT)

    spend = sum(r["cost_vat"] for r in rows)
    print(
        f"[bjorn/ads] {len(rows)} строк, {len({r['ad_id'] for r in rows})} объявлений, "
        f"расход {spend:.0f} ₽ за {date_from}..{date_to}"
    )


if __name__ == "__main__":
    main()
