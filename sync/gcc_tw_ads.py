# -*- coding: utf-8 -*-
"""sync/gcc_tw_ads.py — расход рекламы GCC по кампаниям из SQL-эндпоинта Triple Whale.

Зачем: `summary-page` отдаёт расход ОДНОЙ цифрой на площадку и на весь магазин, поэтому
61% расхода GCC (в основном Meta) шёл строкой без страны и без кампании и не мержился
с трафиком. SQL-эндпоинт TW отдаёт ту же цифру в разрезе кампаний, а имена кампаний
у LIME несут страну (`CPO_SUMMER_SALE_KSA`) — отсюда гео для 63% расхода Meta без
единого нового доступа.

POST https://api.triplewhale.com/api/v2/orcabase/api/sql
Заголовок `x-api-key` (НЕ Bearer — Bearer отвечает 401 Invalid iss), тело
{shopId, query, period:{startDate,endDate}}, в SQL параметры @startDate / @endDate.

⚠️ ВАЛЮТА. В `ads_table` колонка `currency` у facebook-ads = USD, у google-ads = AED,
но само поле `spend` у обоих уже в валюте магазина (AED). Доказательство: ROAS
из интерфейса TW = CV / spend при CV в AED (Meta 11 073,92/10 543,76 = 1,05 = показанный
ROAS; Google 76 481,39/1 982,48 = 38,58 = показанный). То есть `currency` описывает
биллинг кабинета, а не единицу `spend`. Принять её за единицу = завысить Meta в 3.67 раза.
"""
import os
import re
import time

import requests

SQL_URL = "https://api.triplewhale.com/api/v2/orcabase/api/sql"

# Площадка TW → таксономия дашборда. Ключи совпадают с map_tw_source/map_metrika_channel.
# google-ads НЕ здесь намеренно: его расход берём из кабинета (sync/gcc_google_geo.py),
# там гео измеренное (LOCATION_OF_PRESENCE), а не выведенное из имени кампании.
CHANNEL_MAP = {
    "facebook-ads": ("SMM paid", "Meta Ads"),
    "tiktok-ads": ("SMM paid", "TikTok Ads"),
    "snapchat-ads": ("SMM paid", "Snapchat Ads"),
    "pinterest-ads": ("SMM paid", "Pinterest Ads"),
    "bing": ("SEM", "Bing"),
}

# Суффикс страны в имени кампании LIME → название для дашборда.
# Именование задано командой LIME: CPO_SUMMER_SALE_UAE / _KSA / _QAT / _KWT / _OMN.
CAMPAIGN_COUNTRY_SUFFIX = {
    "UAE": "ОАЭ", "AE": "ОАЭ",
    "KSA": "Саудовская Аравия", "SAU": "Саудовская Аравия", "SA": "Саудовская Аравия",
    "QAT": "Катар", "QA": "Катар",
    "KWT": "Кувейт", "KW": "Кувейт",
    "OMN": "Оман", "OM": "Оман",
    "BHR": "Бахрейн", "BH": "Бахрейн",
}

SPEND_SQL = (
    "SELECT event_date, channel, campaign_id, campaign_name, SUM(spend) AS spend "
    "FROM ads_table "
    "WHERE event_date BETWEEN @startDate AND @endDate "
    "GROUP BY event_date, channel, campaign_id, campaign_name"
)


def country_from_campaign_name(name: str | None) -> str | None:
    """Страна из суффикса имени кампании либо None.

    ⚠️ Это НАМЕРЕНИЕ таргетинга, а не измеренная география показа: у Google из кабинета
    страна приходит по LOCATION_OF_PRESENCE, здесь — по тому, как маркетолог назвал
    кампанию. Смешивать их в одной колонке — осознанный компромисс; кампании без
    странового суффикса (`CPO_Catalog_All`) остаются без страны, а не размазываются.
    """
    text = (name or "").upper()
    for suffix, country in CAMPAIGN_COUNTRY_SUFFIX.items():
        # Граница слова с обеих сторон: иначе "SA" поймает "SALE", а "AE" — "AED".
        if re.search(r"(?:^|[_\s\-])" + suffix + r"(?:$|[_\s\-])", text):
            return country
    return None


def tw_sql(api_key: str, shop: str, query: str, date_from: str, date_to: str,
           retries: int = 4) -> list[dict]:
    """Выполнить SELECT в хранилище TW. Транзиентные сбои повторяем — TW рвёт соединение."""
    body = {"shopId": shop, "query": query,
            "period": {"startDate": date_from, "endDate": date_to}}
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(SQL_URL, headers=headers, json=body, timeout=180)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last = exc
        else:
            if resp.status_code < 500 and resp.status_code != 429:
                resp.raise_for_status()
                return resp.json() or []
            last = requests.exceptions.HTTPError(f"TW SQL HTTP {resp.status_code}")
        if attempt < retries:
            time.sleep(5 * attempt)
    assert last is not None
    raise last


def ads_spend_rows(db_rows: list[dict]) -> list[dict]:
    """Строки `ads_table` → строки расхода для merge_rows (в AED, конверт делает оркестратор).

    Google Ads пропускаем: его расход приходит из кабинета с измеренным гео. Если кабинет
    почему-то молчит, оркестратор сам оставит ga_adCost из summary-page — двойного счёта
    не возникает, потому что здесь google-ads не появляется никогда.

    Returns:
        [{date, country, campaign_id, campaign_name, channel, subchannel, traffic_type, cost}]
    """
    out: list[dict] = []
    for row in db_rows:
        channel_raw = (row.get("channel") or "").strip().lower()
        mapped = CHANNEL_MAP.get(channel_raw)
        if not mapped:
            continue
        cost = float(row.get("spend") or 0)
        if not cost:
            continue
        channel, subchannel = mapped
        name = (row.get("campaign_name") or "").strip()
        out.append({
            "date": str(row.get("event_date") or "")[:10],
            "country": country_from_campaign_name(name),
            "campaign_id": (row.get("campaign_id") or "").strip() or None,
            "campaign_name": name,
            "channel": channel,
            "subchannel": subchannel,
            "traffic_type": "Платный",
            "cost": cost,
        })
    return out


def fetch_ads_spend(api_key: str, shop: str, date_from: str, date_to: str) -> list[dict]:
    """Расход по кампаниям за период (AED), готовый к мержу."""
    return ads_spend_rows(tw_sql(api_key, shop, SPEND_SQL, date_from, date_to))


def spend_metrics_covered() -> set[str]:
    """metricId из summary-page, которые перекрыты этим модулем.

    Оркестратор обязан выбросить их из summary-page, иначе тот же расход посчитается
    дважды — ровно те грабли, что уже прошли с ga_adCost и кабинетом Google.
    """
    return {"fb_ads_spend", "totalTiktokSpend", "totalSnapchatSpend",
            "totalPinterestSpend", "totalBingSpend"}
