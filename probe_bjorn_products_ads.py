# Проба под две задачи дашборда BJORN:
#   A. Товарная воронка (просмотр товара → корзина → покупка) в разрезе источников —
#      какие товарные метрики Метрики реально отдаёт счётчик Bjorn и с какими измерениями
#      их можно скрестить. Сейчас синкается только покупка (productPurchasedQuantity/Price).
#   B. Статистика по объявлениям с картинками — отдаёт ли Директ срез до AdId, есть ли
#      у объявлений хеши картинок и превью-ссылки, и чем связать объявление с покупкой.
# Read-only: только GET Метрики и get/reports Директа. Формы enum вскрываются bogus-трюком.
from __future__ import annotations

import json
import os
import time
from typing import Any

import requests

METRIKA_URL = "https://api-metrika.yandex.net/stat/v1/data"
DIRECT_V5 = "https://api.direct.yandex.com/json/v5"
DIRECT_V501 = "https://api.direct.yandex.com/json/v501"

DATE1 = os.environ.get("PROBE_DATE1", "2026-07-01")
DATE2 = os.environ.get("PROBE_DATE2", "2026-08-25")


# ─────────────────────────── A. Метрика ───────────────────────────

def metrika(params: dict[str, Any], token: str) -> tuple[int, dict[str, Any]]:
    resp = requests.get(
        METRIKA_URL,
        params=params,
        headers={"Authorization": f"OAuth {token}"},
        timeout=120,
    )
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {"raw": resp.text[:400]}


def m_short_error(body: dict[str, Any]) -> str:
    msg = body.get("message") or ""
    errs = body.get("errors") or []
    detail = "; ".join(str(e.get("message", ""))[:160] for e in errs[:2])
    return f"{msg} | {detail}"[:260]


def probe_metrika() -> None:
    token = os.environ["METRICA_TOKEN"]
    counter = os.environ["METRICA_COUNTER_ID"]
    base = {
        "ids": counter,
        "date1": DATE1,
        "date2": DATE2,
        "accuracy": "full",
        "proposed_accuracy": "false",
        "attribution": "automatic",
        "lang": "ru",
        "limit": 5,
    }

    print("=" * 78)
    print(f"A. МЕТРИКА · счётчик {counter} · период {DATE1}..{DATE2}")
    print("=" * 78)

    print("\n--- A1. Какие товарные метрики принимает счётчик (по одной) ---")
    metric_candidates = [
        # покупка (уже синкается)
        "ym:s:productPurchasedQuantity",
        "ym:s:productPurchasedPrice",
        "ym:s:productPurchasedUniq",
        # корзина
        "ym:s:productBasketsQuantity",
        "ym:s:productBasketsPrice",
        "ym:s:productBasketsUniq",
        # показы товара в списках / просмотр карточки
        "ym:s:productImpressions",
        "ym:s:productImpressionsQuantity",
        "ym:s:productImpressionsUniq",
        "ym:s:productDetails",
        "ym:s:productDetailsQuantity",
        "ym:s:productDetailsUniq",
        "ym:s:productViews",
        "ym:s:productViewsUniq",
        # общие ecommerce
        "ym:s:ecommercePurchases",
        "ym:s:ecommerceRevenue",
        # заведомо несуществующая — эталон формы ошибки
        "ym:s:productBogusMetric",
    ]
    ok_metrics: list[str] = []
    for metric in metric_candidates:
        status, body = metrika({**base, "metrics": metric, "dimensions": "ym:s:productName"}, token)
        if status == 200:
            total = (body.get("totals") or [None])[0]
            rows = len(body.get("data") or [])
            print(f"  OK   {metric:<38} итог={total} строк={rows}")
            ok_metrics.append(metric)
        else:
            print(f"  {status}  {metric:<38} {m_short_error(body)}")
        time.sleep(0.4)

    print("\n--- A2. Товарные измерения ---")
    dim_candidates = [
        "ym:s:productName",
        "ym:s:productID",
        "ym:s:productBrand",
        "ym:s:productCategory",
        "ym:s:productCategoryLevel1",
        "ym:s:productVariant",
        "ym:s:productCoupon",
        "ym:s:productPosition",
    ]
    probe_metric = ok_metrics[0] if ok_metrics else "ym:s:visits"
    for dim in dim_candidates:
        status, body = metrika({**base, "metrics": probe_metric, "dimensions": dim}, token)
        if status == 200:
            data = body.get("data") or []
            names = [str((d.get("dimensions") or [{}])[0].get("name"))[:28] for d in data[:3]]
            print(f"  OK   {dim:<34} строк={len(data)} примеры={names}")
        else:
            print(f"  {status}  {dim:<34} {m_short_error(body)}")
        time.sleep(0.4)

    print("\n--- A3. Скрещивание: товар × источник × кампания (воронка по источникам) ---")
    funnel_metrics = [m for m in ok_metrics if m.endswith(("Quantity", "Uniq", "Price"))][:6]
    if funnel_metrics:
        dims = "ym:s:date,ym:s:lastsignTrafficSource,ym:s:lastsignUTMCampaign,ym:s:productName"
        status, body = metrika(
            {**base, "metrics": ",".join(funnel_metrics), "dimensions": dims, "limit": 5},
            token,
        )
        print(f"  метрики: {funnel_metrics}")
        if status == 200:
            print(f"  OK  totals={body.get('totals')} всего строк={body.get('total_rows')}")
            for d in (body.get("data") or [])[:3]:
                print("   ", [x.get("name") for x in d.get("dimensions", [])], d.get("metrics"))
        else:
            print(f"  {status} {m_short_error(body)}")
    else:
        print("  пропущено: ни одна товарная метрика не прошла A1")

    print("\n--- A4. Чем связать объявление с покупкой: UTM Content и Директ-измерения ---")
    link_dims = [
        "ym:s:lastsignUTMContent",
        "ym:s:lastsignUTMTerm",
        "ym:s:lastsignDirectBanner",
        "ym:s:lastsignDirectBannerGroup",
        "ym:s:lastsignDirectOrder",
        "ym:s:lastsignDirectID",
        "ym:s:lastsignDirectPlatformType",
        "ym:ad:directBanner",
        "ym:ad:directBannerGroup",
    ]
    link_metric = "ym:s:productPurchasedQuantity" if "ym:s:productPurchasedQuantity" in ok_metrics else probe_metric
    for dim in link_dims:
        metric = "ym:ad:visits" if dim.startswith("ym:ad:") else link_metric
        status, body = metrika({**base, "metrics": metric, "dimensions": dim, "limit": 6}, token)
        if status == 200:
            data = body.get("data") or []
            names = [str((d.get("dimensions") or [{}])[0].get("name"))[:34] for d in data[:4]]
            print(f"  OK   {dim:<36} строк={len(data)} примеры={names}")
        else:
            print(f"  {status}  {dim:<36} {m_short_error(body)}")
        time.sleep(0.4)


# ─────────────────────────── B. Директ ───────────────────────────

def direct_clients() -> list[dict[str, str]]:
    default_token = os.environ.get("DIRECT_TOKEN", "")
    raw = os.environ.get("DIRECT_CLIENTS_JSON", "")
    if raw:
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
    return [{"login": os.environ["DIRECT_CLIENT_LOGIN"], "token": default_token}]


def direct_call(service: str, params: dict, login: str, token: str, base: str = DIRECT_V5) -> dict:
    resp = requests.post(
        f"{base}/{service}",
        headers={
            "Authorization": f"Bearer {token}",
            "Client-Login": login,
            "Accept-Language": "ru",
            "Content-Type": "application/json; charset=utf-8",
        },
        data=json.dumps({"method": "get", "params": params}, ensure_ascii=False).encode("utf-8"),
        timeout=120,
    )
    try:
        return resp.json()
    except Exception:
        return {"error": {"error_code": resp.status_code, "error_string": "не JSON", "error_detail": resp.text[:300]}}


def d_err(body: dict) -> str | None:
    e = body.get("error")
    if not e:
        return None
    return f"ERROR {e.get('error_code')} {e.get('error_string')}: {str(e.get('error_detail'))[:400]}"


def direct_report(login: str, token: str, fields: list[str], limit: int = 12) -> tuple[int, str]:
    body = {
        "params": {
            "SelectionCriteria": {"DateFrom": DATE1, "DateTo": DATE2},
            "FieldNames": fields,
            "ReportName": f"probe_{login}_{'_'.join(fields)[:40]}_{DATE1}",
            "ReportType": "CUSTOM_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES",
            "Page": {"Limit": limit},
        }
    }
    for _ in range(20):
        resp = requests.post(
            f"{DIRECT_V5}/reports",
            headers={
                "Authorization": f"Bearer {token}",
                "Client-Login": login,
                "Accept-Language": "ru",
                "processingMode": "auto",
                "returnMoneyInMicros": "false",
                "skipReportHeader": "true",
                "skipColumnHeader": "false",
                "skipReportSummary": "true",
                "Content-Type": "application/json; charset=utf-8",
            },
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            timeout=180,
        )
        if resp.status_code in (201, 202):
            time.sleep(10)
            continue
        return resp.status_code, resp.text[:2500]
    return 0, "таймаут ожидания отчёта"


def probe_direct() -> None:
    print("\n" + "=" * 78)
    print(f"B. ДИРЕКТ · период {DATE1}..{DATE2}")
    print("=" * 78)

    for client in direct_clients():
        login, token = client["login"], client["token"]
        print(f"\n########## Кабинет {login} ##########")

        print("\n--- B1. Допустимые поля отчёта (bogus-трюк) ---")
        status, text = direct_report(login, token, ["Bogus"])
        print(f"  HTTP {status}: {text[:1200]}")

        print("\n--- B2. Срез до объявления: Date/Campaign/AdGroup/Ad + показы/клики/расход ---")
        status, text = direct_report(
            login,
            token,
            ["Date", "CampaignId", "CampaignName", "AdGroupId", "AdId", "Impressions", "Clicks", "Cost"],
        )
        print(f"  HTTP {status}")
        print("\n".join(("  " + ln) for ln in text.splitlines()[:8]))

        print("\n--- B3. Есть ли в отчёте поле картинки / формата объявления ---")
        for extra in (["Date", "AdId", "AdImageHash", "Impressions"], ["Date", "AdId", "AdFormat", "Impressions"], ["Date", "AdId", "Conversions", "Impressions"]):
            status, text = direct_report(login, token, extra, limit=4)
            head = text.splitlines()[0][:220] if text else ""
            print(f"  {extra} → HTTP {status} | {head}")

        print("\n--- B4. Типы кампаний в кабинете ---")
        camps = direct_call(
            "campaigns",
            {"SelectionCriteria": {}, "FieldNames": ["Id", "Name", "Type", "State"], "Page": {"Limit": 200}},
            login,
            token,
        )
        if d_err(camps):
            print("  ", d_err(camps))
            continue
        rows = camps["result"].get("Campaigns", [])
        by_type: dict[str, int] = {}
        for c in rows:
            by_type[c.get("Type", "?")] = by_type.get(c.get("Type", "?"), 0) + 1
        print(f"  всего кампаний={len(rows)} по типам={by_type}")
        camp_ids = [c["Id"] for c in rows][:40]

        print("\n--- B5. Объявления: типы, хеши картинок, ссылки (utm) ---")
        ads = direct_call(
            "ads",
            {
                "SelectionCriteria": {"CampaignIds": camp_ids},
                "FieldNames": ["Id", "CampaignId", "AdGroupId", "Type", "State", "Status"],
                "TextAdFieldNames": ["Title", "Text", "Href", "AdImageHash"],
                "TextImageAdFieldNames": ["AdImageHash", "Href"],
                "ImageAdFieldNames": ["AdImageHash", "Href"],
                "SmartAdBuilderAdFieldNames": ["Href"],
                "Page": {"Limit": 200},
            },
            login,
            token,
        )
        if d_err(ads):
            print("  ", d_err(ads))
        else:
            ad_rows = ads["result"].get("Ads", [])
            by_ad_type: dict[str, int] = {}
            hashes: list[str] = []
            sample_href = ""
            for a in ad_rows:
                by_ad_type[a.get("Type", "?")] = by_ad_type.get(a.get("Type", "?"), 0) + 1
                for block in ("TextAd", "TextImageAd", "ImageAd"):
                    h = (a.get(block) or {}).get("AdImageHash")
                    if h:
                        hashes.append(h)
                    href = (a.get(block) or {}).get("Href")
                    if href and not sample_href:
                        sample_href = href
            print(f"  объявлений={len(ad_rows)} по типам={by_ad_type}")
            print(f"  с картинкой={len(hashes)} уникальных хешей={len(set(hashes))}")
            print(f"  пример ссылки (utm): {sample_href[:300]}")

            print("\n--- B6. adimages: превью и оригинал по хешу ---")
            print("  поля сервиса:", d_err(direct_call("adimages", {"SelectionCriteria": {}, "FieldNames": ["Bogus"], "Page": {"Limit": 1}}, login, token, DIRECT_V501)))
            if hashes:
                imgs = direct_call(
                    "adimages",
                    {
                        "SelectionCriteria": {"AdImageHashes": list(set(hashes))[:10]},
                        "FieldNames": ["AdImageHash", "Name", "Type", "Subtype", "PreviewUrl", "OriginalUrl"],
                        "Page": {"Limit": 10},
                    },
                    login,
                    token,
                    DIRECT_V501,
                )
                if d_err(imgs):
                    print("  ", d_err(imgs))
                else:
                    for im in imgs["result"].get("AdImages", [])[:5]:
                        print("   ", json.dumps(im, ensure_ascii=False)[:400])

            print("\n--- B7. Активы ЕПК (assets) — картинки унифицированных кампаний ---")
            print("  поля assets:", d_err(direct_call("assets", {"SelectionCriteria": {}, "FieldNames": ["Bogus"], "Page": {"Limit": 1}}, login, token, DIRECT_V501)))


if __name__ == "__main__":
    if os.environ.get("METRICA_TOKEN"):
        probe_metrika()
    else:
        print("METRICA_TOKEN не задан — часть A пропущена")
    if os.environ.get("DIRECT_CLIENTS_JSON") or os.environ.get("DIRECT_CLIENT_LOGIN"):
        probe_direct()
    else:
        print("DIRECT_* не заданы — часть B пропущена")
