# -*- coding: utf-8 -*-
"""
sync/lime_geomedia.py — синк Геомедийки (Яндекс Геореклама, Баннеры/Билборды в Картах)
→ lime_media_stats. API нет; статистику отдаёт внутренний эндпоинт кабинета
getStatisticsSummary. POST к нему требует CSRF-заголовок, который проставляет сам сайт,
поэтому НЕ дёргаем эндпоинт напрямую, а открываем страницу статистики с фильтром на нужный
день (dateStart=dateEnd=день) и читаем ответ, который страница загружает сама — как при
обычном просмотре. Эндпоинт даёт ТОТАЛ за период → навигация per-day = дневной грейн.

  data.campaignsStatistics[]: {campaignId, campaignName, productName,
       details:{clicks, ctr, shows, spent, openSites, phoneCalls, searches}}
Пишем source='geo.media' (impressions=shows, clicks, cost=spent RUB).

Запуск:  python -m sync.lime_geomedia     (DRY: MEDIA_DRY=1)
ENV: DATABASE_URL, YANDEX_STORAGE_STATE, LIME_GEO_COMPANY_ID (93296554), LIME_GEO_DAYS_BACK (14)
"""
import os
import json
import traceback
from datetime import date, timedelta

from sync.lime_media_session import yandex_page

COMPANY_ID = os.environ.get("LIME_GEO_COMPANY_ID", "93296554")
ORIGIN = f"https://yandex.ru/geoadv/statistics?company_id={COMPANY_ID}"


def _day_url(day: str) -> str:
    # фильтр в URL страницы → сайт сам грузит статистику за этот день (со своим CSRF)
    return (f"https://yandex.ru/geoadv/statistics?company_id={COMPANY_ID}"
            f"&filters=activeTab%3Acampaigns%3BdateStart%3A{day}%3BdateEnd%3A{day}")


def _media_type(product_name: str) -> str:
    n = (product_name or "").lower()
    if "билборд" in n or "билблорд" in n:  # в кабинете встречается опечатка «Билблорд»
        return "Билборды"
    if "баннер" in n:
        return "Баннеры"
    return ""


def _num(v):
    try:
        return float(str(v).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def fetch_day(page, day: str) -> list:
    # навигируем на страницу с фильтром дня и ловим ответ getStatisticsSummary
    with page.expect_response(
        lambda r: "getStatisticsSummary" in r.url, timeout=45000
    ) as info:
        page.goto(_day_url(day), wait_until="commit", timeout=60000)
    resp = info.value
    if resp.status != 200:
        raise RuntimeError(f"getStatisticsSummary HTTP {resp.status}")
    data = resp.json()
    return (data.get("data") or {}).get("campaignsStatistics") or []


def build_rows(page, date_from: date, date_to: date) -> list:
    rows = []
    d = date_from
    while d <= date_to:
        day = d.isoformat()
        try:
            camps = fetch_day(page, day)
        except Exception as e:
            print(f"[geo] день {day}: {e}")
            d += timedelta(days=1)
            continue
        for c in camps:
            det = c.get("details") or {}
            shows = int(_num(det.get("shows")))
            clicks = int(_num(det.get("clicks")))
            spent = round(_num(det.get("spent")), 2)
            if shows == 0 and clicks == 0 and spent == 0:
                continue  # день без открутки по кампании — не засоряем
            name = str(c.get("campaignName", "")).strip() or "geo.media"
            rows.append({
                "date": day, "region": "ru", "source": "geo.media",
                "campaign_group": name, "media_type": _media_type(c.get("productName")),
                "campaign_id": str(c.get("campaignId")) if c.get("campaignId") else None,
                "impressions": shows, "reach": 0, "clicks": clicks,
                "cost": spent, "currency": str(c.get("currency") or "RUB"),
                "video_completes": 0, "vtr": None, "cpv": None,
                "conversions": json.dumps({}, ensure_ascii=False),
            })
        d += timedelta(days=1)
    return rows


def main() -> None:
    days_back = int(os.environ.get("LIME_GEO_DAYS_BACK", "14"))
    date_to = date.today() - timedelta(days=1)
    date_from = date_to - timedelta(days=days_back - 1)
    print(f"[geo] company={COMPANY_ID} период {date_from}..{date_to}")

    with yandex_page(ORIGIN) as page:
        rows = build_rows(page, date_from, date_to)

    print(f"[geo] строк к записи: {len(rows)}")
    if rows[:1]:
        print(f"[geo] пример: {rows[0]}")

    if os.environ.get("MEDIA_DRY") == "1":
        print("[geo] DRY — в БД не пишу")
        return

    from sync.lime_urban import ensure_media_schema, upsert_media
    ensure_media_schema()
    n = upsert_media(rows)
    print(f"[geo] upsert в lime_media_stats: {n} строк (source=geo.media)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[geo] ОШИБКА: {e}")
        traceback.print_exc()
        raise
