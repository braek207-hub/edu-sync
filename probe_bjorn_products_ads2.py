# Проба BJORN, раунд 2. Раунд 1 доказал: товарная воронка Метрики (показы→корзина→покупка)
# скрещивается с источником, а ym:s:<attr>DirectBanner = AdId Директа. Осталось закрыть:
#   B5. Правильные *FieldNames сервиса ads (в раунде 1 упало на ImageAdFieldNames);
#   B6. adimages/creatives — есть ли превью-ссылка на картинку;
#   A5. Товарная воронка В РАЗРЕЗЕ ОБЪЯВЛЕНИЯ (товар × DirectBanner) — сколько строк, сходятся ли.
# Read-only.
from __future__ import annotations

import json
import os
import time
from typing import Any

import requests

METRIKA_URL = "https://api-metrika.yandex.net/stat/v1/data"
DIRECT_V5 = "https://api.direct.yandex.com/json/v5"

DATE1 = os.environ.get("PROBE_DATE1", "2026-07-01")
DATE2 = os.environ.get("PROBE_DATE2", "2026-08-25")


def metrika(params: dict[str, Any], token: str) -> tuple[int, dict[str, Any]]:
    resp = requests.get(METRIKA_URL, params=params, headers={"Authorization": f"OAuth {token}"}, timeout=180)
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {"raw": resp.text[:400]}


def probe_metrika_ads() -> None:
    token = os.environ["METRICA_TOKEN"]
    counter = os.environ["METRICA_COUNTER_ID"]
    base = {
        "ids": counter,
        "date1": DATE1,
        "date2": DATE2,
        "accuracy": "full",
        "proposed_accuracy": "false",
        "attribution": "lastsign",
        "lang": "ru",
    }
    prod_metrics = (
        "ym:s:productImpressions,ym:s:productImpressionsUniq,"
        "ym:s:productBasketsQuantity,ym:s:productBasketsUniq,"
        "ym:s:productPurchasedQuantity,ym:s:productPurchasedPrice"
    )

    print("=" * 78)
    print("A5. Товарная воронка в разрезе ОБЪЯВЛЕНИЯ (товар × DirectBanner)")
    print("=" * 78)
    status, body = metrika(
        {**base, "metrics": prod_metrics, "dimensions": "ym:s:lastsignDirectBanner,ym:s:productName", "limit": 6},
        token,
    )
    print(f"  HTTP {status} строк всего={body.get('total_rows')} totals={body.get('totals')}")
    for d in (body.get("data") or [])[:6]:
        print("   ", [x.get("name") for x in d.get("dimensions", [])], d.get("metrics"))

    print("\n--- A6. Дневная грань: date × DirectBanner × товар (объём витрины) ---")
    status, body = metrika(
        {**base, "metrics": prod_metrics, "dimensions": "ym:s:date,ym:s:lastsignDirectBanner,ym:s:productName", "limit": 3},
        token,
    )
    print(f"  HTTP {status} строк всего={body.get('total_rows')} (за {DATE1}..{DATE2})")

    print("\n--- A7. Дневная грань БЕЗ товара: date × DirectBanner (визиты/корзины/покупки объявления) ---")
    status, body = metrika(
        {
            **base,
            "metrics": "ym:s:visits,ym:s:users,ym:s:bounceRate," + prod_metrics,
            "dimensions": "ym:s:date,ym:s:lastsignDirectBanner",
            "limit": 3,
        },
        token,
    )
    print(f"  HTTP {status} строк всего={body.get('total_rows')} totals={body.get('totals')}")
    for d in (body.get("data") or [])[:3]:
        print("   ", [x.get("name") for x in d.get("dimensions", [])], d.get("metrics"))

    print("\n--- A8. Дневная грань: date × источник × товар (витрина товарной воронки) ---")
    status, body = metrika(
        {**base, "metrics": prod_metrics, "dimensions": "ym:s:date,ym:s:lastsignTrafficSource,ym:s:lastsignUTMCampaign,ym:s:productName", "limit": 3},
        token,
    )
    print(f"  HTTP {status} строк всего={body.get('total_rows')}")

    print("\n--- A9. Разрез страницы входа / категории (куда переходят с объявления) ---")
    for dim in ("ym:s:startURL", "ym:s:productCategoryLevel1", "ym:s:productID"):
        status, body = metrika({**base, "metrics": prod_metrics, "dimensions": f"ym:s:lastsignDirectBanner,{dim}", "limit": 3}, token)
        print(f"  {dim:<32} HTTP {status} строк={body.get('total_rows')}")
        time.sleep(0.4)


def direct_clients() -> list[dict[str, str]]:
    default_token = os.environ.get("DIRECT_TOKEN", "")
    raw = os.environ.get("DIRECT_CLIENTS_JSON", "")
    clients: list[dict[str, str]] = []
    for item in json.loads(raw):
        if isinstance(item, str):
            clients.append({"login": item.strip(), "token": default_token})
        elif isinstance(item, dict):
            login = str(item.get("login") or item.get("client_login") or "").strip()
            token = str(item.get("token") or "").strip() or default_token
            if login:
                clients.append({"login": login, "token": token})
    return clients


def direct_call(service: str, params: dict, login: str, token: str, method: str = "get") -> dict:
    resp = requests.post(
        f"{DIRECT_V5}/{service}",
        headers={
            "Authorization": f"Bearer {token}",
            "Client-Login": login,
            "Accept-Language": "ru",
            "Content-Type": "application/json; charset=utf-8",
        },
        data=json.dumps({"method": method, "params": params}, ensure_ascii=False).encode("utf-8"),
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


def probe_direct_images() -> None:
    print("\n" + "=" * 78)
    print("B. ДИРЕКТ · объявления и картинки")
    print("=" * 78)

    for client in direct_clients():
        login, token = client["login"], client["token"]
        print(f"\n########## Кабинет {login} ##########")

        camps = direct_call(
            "campaigns",
            {"SelectionCriteria": {}, "FieldNames": ["Id", "Name", "Type"], "Page": {"Limit": 300}},
            login,
            token,
        )
        if d_err(camps):
            print("  ", d_err(camps))
            continue
        camp_ids = [c["Id"] for c in camps["result"].get("Campaigns", [])]
        print(f"  кампаний={len(camp_ids)}")

        print("\n--- B5. ads.get: типы объявлений и хеши картинок ---")
        all_ads: list[dict] = []
        offset = 0
        while True:
            ads = direct_call(
                "ads",
                {
                    "SelectionCriteria": {"CampaignIds": camp_ids[:1000]},
                    "FieldNames": ["Id", "CampaignId", "AdGroupId", "Type", "Subtype", "State", "Status"],
                    "TextAdFieldNames": ["Title", "Title2", "Text", "Href", "AdImageHash"],
                    "TextImageAdFieldNames": ["AdImageHash", "Href"],
                    "TextAdBuilderAdFieldNames": ["Creative", "Href"],
                    "DynamicTextAdFieldNames": ["Text", "AdImageHash"],
                    "SmartAdBuilderAdFieldNames": ["Creative"],
                    "Page": {"Limit": 10000, "Offset": offset},
                },
                login,
                token,
            )
            if d_err(ads):
                print("  ", d_err(ads))
                break
            chunk = ads["result"].get("Ads", [])
            all_ads.extend(chunk)
            limited = (ads["result"].get("LimitedBy") or 0)
            if not limited:
                break
            offset = limited
            time.sleep(0.5)

        by_type: dict[str, int] = {}
        hashes: list[str] = []
        creative_ids: list[int] = []
        sample_href = ""
        sample_ad: dict = {}
        for a in all_ads:
            by_type[a.get("Type", "?")] = by_type.get(a.get("Type", "?"), 0) + 1
            for block in ("TextAd", "TextImageAd", "DynamicTextAd"):
                node = a.get(block) or {}
                if node.get("AdImageHash"):
                    hashes.append(node["AdImageHash"])
                    if not sample_ad:
                        sample_ad = {"Id": a.get("Id"), "Type": a.get("Type"), block: node}
                if node.get("Href") and not sample_href:
                    sample_href = node["Href"]
            for block in ("TextAdBuilderAd", "SmartAdBuilderAd"):
                cr = ((a.get(block) or {}).get("Creative") or {})
                if cr.get("CreativeId"):
                    creative_ids.append(cr["CreativeId"])
        print(f"  объявлений={len(all_ads)} по типам={by_type}")
        print(f"  с AdImageHash={len(hashes)} уникальных={len(set(hashes))}; креативов конструктора={len(set(creative_ids))}")
        print(f"  пример ссылки: {sample_href[:260]}")
        if sample_ad:
            print(f"  пример объявления с картинкой: {json.dumps(sample_ad, ensure_ascii=False)[:400]}")

        print("\n--- B6. adimages.get: превью и оригинал ---")
        if hashes:
            imgs = direct_call(
                "adimages",
                {
                    "SelectionCriteria": {"AdImageHashes": list(dict.fromkeys(hashes))[:20]},
                    "FieldNames": ["AdImageHash", "Name", "Type", "Subtype", "PreviewUrl", "OriginalUrl"],
                    "Page": {"Limit": 20},
                },
                login,
                token,
            )
            if d_err(imgs):
                print("  ", d_err(imgs))
            else:
                got = imgs["result"].get("AdImages", [])
                print(f"  вернулось картинок={len(got)}")
                for im in got[:4]:
                    print("   ", json.dumps(im, ensure_ascii=False)[:420])
        else:
            print("  хешей нет — пропуск")

        print("\n--- B7. creatives.get: превью креативов конструктора ---")
        if creative_ids:
            crs = direct_call(
                "creatives",
                {
                    "SelectionCriteria": {"Ids": list(dict.fromkeys(creative_ids))[:20]},
                    "FieldNames": ["Id", "Type", "PreviewUrl", "ThumbnailUrl", "Name"],
                    "Page": {"Limit": 20},
                },
                login,
                token,
            )
            if d_err(crs):
                print("  ", d_err(crs))
            else:
                got = crs["result"].get("Creatives", [])
                print(f"  вернулось креативов={len(got)}")
                for c in got[:4]:
                    print("   ", json.dumps(c, ensure_ascii=False)[:420])
        else:
            print("  креативов конструктора нет — пропуск")


if __name__ == "__main__":
    probe_metrika_ads()
    probe_direct_images()
