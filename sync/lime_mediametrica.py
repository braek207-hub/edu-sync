# -*- coding: utf-8 -*-
"""
sync/lime_mediametrica.py — синк Медиаметрики (AdMetrica, post-view) → lime_media_stats.

ПРЯМОЙ ОФИЦИАЛЬНЫЙ API (переезд 2026-08-20 с headless-обхода). Хост
api.media.metrika.yandex.net/v1, OAuth-токен. Доход/охват/воронка/видео берутся
документированными метриками БЕЗ «Метрики Про» — раньше их гейтил внутренний API кабинета
(имена вроде am:e:postViewRevenue → 4002), а официальный принимает правильные имена:
  - am:e:ecommerce<currency>RevenuePostView — ПОСТ-ВЬЮ ДОХОД (раньше был 0/оценка);
  - am:e:goal<id>Reaches — пост-вью конверсии по цели (покупка/корзина/оформление);
  - am:e:renders(показы) / am:e:users(охват) / am:e:clicks;
  - am:e:videoComplete — досмотры видео.
Всё одним запросом /stat/data на кампанию с dimensions=am:e:date (дневной грейн).

⚠️ ids в stat/data = campaign_id из /management/campaigns (внутренний AdMetrica id,
НЕ counter, НЕ Директ id). Со counter возвращались нули.

Пишем в lime_media_stats source='mediametrica'. cost=0 — расход медийки берётся из
Директа/Urban; ценность Медиаметрики = ОХВАТ + POST-VIEW доход/конверсии + досмотры.

Запуск:  python -m sync.lime_mediametrica     (DRY: MEDIA_DRY=1 — печать без записи в БД)
ENV: DATABASE_URL, LIME_MM_OAUTH_TOKEN, LIME_MM_DAYS_BACK (14),
     LIME_MM_PURCHASE_GOAL (3023504302), LIME_MM_CART_GOAL (194380276),
     LIME_MM_CHECKOUT_GOAL (340817822), LIME_MM_CURRENCY (RUB)
"""
import os
import json
import traceback
from datetime import date, timedelta

import requests

BASE = "https://api.media.metrika.yandex.net/v1"
ADVERTISER_NAME = "Lime"
CURRENCY = os.environ.get("LIME_MM_CURRENCY", "RUB")
PURCHASE_GOAL = os.environ.get("LIME_MM_PURCHASE_GOAL", "3023504302")  # цель «Покупка»
CART_GOAL = os.environ.get("LIME_MM_CART_GOAL", "194380276")           # «Добавление в корзину»
CHECKOUT_GOAL = os.environ.get("LIME_MM_CHECKOUT_GOAL", "340817822")   # «Начало оформления»

_TOKEN = os.environ.get("LIME_MM_OAUTH_TOKEN", "")


def _headers() -> dict:
    if not _TOKEN:
        raise RuntimeError("LIME_MM_OAUTH_TOKEN не задан — нужен OAuth-токен Яндекса с доступом к AdMetrica")
    return {"Authorization": f"OAuth {_TOKEN}", "Content-Type": "application/x-yametrika+json"}


# Метрики одного запроса (порядок = порядок индексов в ответе).
def _metrics() -> str:
    return ",".join([
        "am:e:renders",                                   # 0 показы
        "am:e:users",                                     # 1 охват
        "am:e:clicks",                                    # 2 клики
        f"am:e:ecommerce{CURRENCY}RevenuePostView",       # 3 пост-вью доход
        f"am:e:goal{PURCHASE_GOAL}Reaches",               # 4 пост-вью покупки
        f"am:e:goal{CART_GOAL}Reaches",                   # 5 пост-вью корзина
        f"am:e:goal{CHECKOUT_GOAL}Reaches",               # 6 пост-вью оформление
        "am:e:videoComplete",                             # 7 досмотры видео (0 на не-видео)
    ])


def _media_type(name: str) -> str:
    n = (name or "").lower()
    if "видео" in n or "video" in n:
        return "Видео"
    if " tv" in n or "тв" in n or name.strip().endswith("TV"):
        return "TV"
    if "баннер" in n or "banner" in n:
        return "Баннеры"
    return ""


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fetch_campaigns() -> list:
    """Список кампаний рекламодателя Lime из официального management API."""
    r = requests.get(f"{BASE}/management/campaigns", headers=_headers(), timeout=30)
    r.raise_for_status()
    camps = r.json().get("campaigns", [])
    return [c for c in camps if str(c.get("advertiser_name", "")).strip() == ADVERTISER_NAME]


def fetch_daily(campaign_id, date1: str, date2: str) -> list:
    """Дневной отчёт по кампании: /stat/data с dimensions=am:e:date. Возвращает data[]."""
    params = {
        "ids": campaign_id,
        "date1": date1,
        "date2": date2,
        "metrics": _metrics(),
        "dimensions": "am:e:date",
        "limit": 1000,
        "sort": "am:e:date",
    }
    r = requests.get(f"{BASE}/stat/data", headers=_headers(), params=params, timeout=45)
    r.raise_for_status()
    return r.json().get("data", [])


def build_rows(campaigns: list, date1: str, date2: str) -> list:
    rows = []
    for c in campaigns:
        cid = c.get("campaign_id")
        name = str(c.get("name", "")).strip()
        # пропускаем кампании, чей флайт не пересекается с окном
        if c.get("date_end") and c["date_end"] < date1:
            continue
        if c.get("date_start") and c["date_start"] > date2:
            continue
        try:
            daily = fetch_daily(cid, date1, date2)
        except Exception as e:
            print(f"[mm] кампания {cid} '{name}': {e}")
            continue
        mtype = _media_type(name)
        for r in daily:
            dims = r.get("dimensions", [{}])
            d = (dims[0].get("name") or dims[0].get("id") or "")[:10] if dims else ""
            if len(d) != 10:
                continue
            m = r.get("metrics", [])
            def g(i):  # безопасный доступ к метрике по индексу
                return _num(m[i]) if len(m) > i else 0.0
            renders = int(g(0))
            users = int(g(1))          # охват
            clicks = int(g(2))
            pv_revenue = round(g(3), 2)   # пост-вью доход (реальный!)
            pv_purchase = int(g(4))
            pv_cart = int(g(5))
            pv_checkout = int(g(6))
            video_completes = int(g(7))
            rows.append({
                "date": d, "region": "ru", "source": "mediametrica",
                "campaign_group": name, "media_type": mtype,
                "campaign_id": str(cid),
                "impressions": renders, "reach": users, "clicks": clicks,
                "cost": 0.0, "currency": CURRENCY,
                "video_completes": video_completes, "vtr": None, "cpv": None,
                "conversions": json.dumps({
                    "pv_purchase": pv_purchase,
                    "pv_cart": pv_cart,
                    "pv_checkout": pv_checkout,
                    "pv_revenue": pv_revenue,
                }, ensure_ascii=False),
            })
    return rows


def main() -> None:
    days_back = int(os.environ.get("LIME_MM_DAYS_BACK", "14"))
    date_to = date.today() - timedelta(days=1)
    date_from = date_to - timedelta(days=days_back - 1)
    d1, d2 = date_from.isoformat(), date_to.isoformat()
    print(f"[mm] период {d1}..{d2}, цель покупки={PURCHASE_GOAL}, валюта={CURRENCY}")

    campaigns = fetch_campaigns()
    print(f"[mm] кампаний Lime: {len(campaigns)}")
    rows = build_rows(campaigns, d1, d2)

    print(f"[mm] строк к записи: {len(rows)}")
    if rows[:1]:
        print(f"[mm] пример: {rows[0]}")
    rev = sum(json.loads(r["conversions"]).get("pv_revenue", 0) for r in rows)
    pur = sum(json.loads(r["conversions"]).get("pv_purchase", 0) for r in rows)
    print(f"[mm] итог за окно: пост-вью доход={rev:,.0f} ₽, покупки={pur}, охват-сумма={sum(r['reach'] for r in rows):,}")

    if os.environ.get("MEDIA_DRY") == "1":
        print("[mm] DRY — в БД не пишу")
        return

    from sync.lime_urban import ensure_media_schema, upsert_media
    ensure_media_schema()
    n = upsert_media(rows)
    print(f"[mm] upsert в lime_media_stats: {n} строк (source=mediametrica)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[mm] ОШИБКА: {e}")
        traceback.print_exc()
        raise
