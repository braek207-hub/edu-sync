# -*- coding: utf-8 -*-
"""
sync/lime_mediametrica.py — синк Медиаметрики (AdMetrica, post-view) → lime_media_stats.

У AdMetrica документированный API только за «Метрика Про» (её нет). Внутренний API кабинета
media.metrika.yandex.ru работает по сохранённой сессии Яндекса БЕЗ «Про» — это те же запросы,
что делает сам сайт при отрисовке отчётов. Ходим headless-браузером (см. lime_media_session):
  - GET /api/v1/campaign/list — кампании рекламодателя Lime (promoterId 17618).
  - GET /api/v1/report/table-data?group=day&goal_id=<покупка> — дневной отчёт:
      renders(показы), users(охват), clicks, goal<ID>Reaches(пост-вью конверсии по цели).

Пишем в lime_media_stats source='mediametrica'. cost тут 0 — расход медийки берётся из
Директа/Urban; ценность Медиаметрики = ОХВАТ + POST-VIEW конверсии.

Запуск:  python -m sync.lime_mediametrica     (DRY: MEDIA_DRY=1 — печать без записи в БД)
ENV: DATABASE_URL, YANDEX_STORAGE_STATE, LIME_MM_DAYS_BACK (14), LIME_MM_PURCHASE_GOAL (3023504302)
"""
import os
import json
import traceback
from datetime import date, timedelta

from sync.lime_media_session import yandex_page, page_fetch_json

ORIGIN = "https://media.metrika.yandex.ru/"
PROMOTER_ID = 17618          # рекламодатель Lime
ADVERTISER_NAME = "Lime"
PURCHASE_GOAL = os.environ.get("LIME_MM_PURCHASE_GOAL", "3023504302")  # цель «Покупка» (клад METRIKA_GOALS)


def _media_type(name: str) -> str:
    n = (name or "").lower()
    if "видео" in n or "video" in n:
        return "Видео"
    if " tv" in n or "тв" in n or name.strip().endswith("TV"):
        return "TV"
    if "баннер" in n or "banner" in n:
        return "Баннеры"
    return ""


def fetch_campaigns(page) -> list:
    data = page_fetch_json(page, "/api/v1/campaign/list?limit=200&offset=0")
    camps = data.get("result", {}).get("campaigns", [])
    return [c for c in camps if str(c.get("advertiserName", "")).strip() == ADVERTISER_NAME]


def fetch_daily(page, campaign_id, date1: str, date2: str) -> list:
    # Литеральный <goal_id> — сервер подставляет из параметра goal_id (иначе HTTP 400).
    metrics = ("am:e:renders,am:e:clicks,am:e:ctr,am:e:users,am:e:renderFrequency,"
               "am:e:goal<goal_id>Reaches,am:e:goal<goal_id>Conversion")
    path = (
        "/api/v1/report/table-data?limit=400&offset=1"
        f"&ids={campaign_id}&metrics={metrics}"
        "&dimensions=am:e:datePeriod<group>&group=day"
        f"&date1={date1}&date2={date2}&goal_id={PURCHASE_GOAL}"
        "&filters=&sort=am:e:datePeriodday"
    )
    data = page_fetch_json(page, path)
    return data.get("result", {}).get("data", [])


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def build_rows(campaigns: list, page, date1: str, date2: str) -> list:
    rows = []
    for c in campaigns:
        cid = c.get("campaignId")
        name = str(c.get("name", "")).strip()
        # пропускаем кампании, чей флайт не пересекается с окном
        if c.get("dateEnd") and c["dateEnd"] < date1:
            continue
        if c.get("dateStart") and c["dateStart"] > date2:
            continue
        try:
            daily = fetch_daily(page, cid, date1, date2)
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
            renders = int(_num(m[0])) if len(m) > 0 else 0
            clicks  = int(_num(m[1])) if len(m) > 1 else 0
            users   = int(_num(m[3])) if len(m) > 3 else 0   # охват
            reaches = int(_num(m[5])) if len(m) > 5 else 0   # пост-вью конверсии по цели «Покупка»
            rows.append({
                "date": d, "region": "ru", "source": "mediametrica",
                "campaign_group": name, "media_type": mtype,
                "campaign_id": str(cid),
                "impressions": renders, "reach": users, "clicks": clicks,
                "cost": 0.0, "currency": "RUB",
                "video_completes": 0, "vtr": None, "cpv": None,
                "conversions": json.dumps({"pv_purchase": reaches}, ensure_ascii=False),
            })
    return rows


def main() -> None:
    days_back = int(os.environ.get("LIME_MM_DAYS_BACK", "14"))
    date_to = date.today() - timedelta(days=1)
    date_from = date_to - timedelta(days=days_back - 1)
    d1, d2 = date_from.isoformat(), date_to.isoformat()
    print(f"[mm] период {d1}..{d2}, цель покупки={PURCHASE_GOAL}")

    with yandex_page(ORIGIN) as page:
        campaigns = fetch_campaigns(page)
        print(f"[mm] кампаний Lime: {len(campaigns)}")
        rows = build_rows(campaigns, page, d1, d2)

    print(f"[mm] строк к записи: {len(rows)}")
    if rows[:2]:
        print(f"[mm] пример: {rows[0]}")

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
