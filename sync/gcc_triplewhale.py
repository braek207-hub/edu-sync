"""Triple Whale-клиент GCC: заказы по каналам (attribution) + расход по каналам (summary-page).

Всё в AED — конверт в ₽ делает оркестратор (B4), не этот модуль.
Каналы/подканалы совпадают с sync.gcc_channels.map_metrika_channel — нужно
для мержа трафика (Метрика) с деньгами (Triple Whale) по (channel, subchannel).
"""
import re
import time

import requests

from sync.gcc_channels import map_domain_country, map_tw_source

_HOST_RE = re.compile(r"https?://([^/]+)", re.I)

TW_ORDERS_URL = "https://api.triplewhale.com/api/v2/attribution/get-orders-with-journeys-v2"
TW_SPEND_URL = "https://api.triplewhale.com/api/v2/summary-page/get-data"

# Ретраи: один транзиентный ReadTimeout у TW ронял весь бэкфилл (516 дней) на середине.
TW_RETRIES = 4
TW_BACKOFF_SEC = 5


def _tw_post(url: str, headers: dict, body: dict, timeout: int) -> dict:
    """POST в Triple Whale с ретраями транзиентных сбоев.

    Повторяем таймауты/обрывы соединения и 5xx/429 с линейным бэкоффом.
    4xx (кроме 429) — постоянная ошибка (неверный ключ/тело), падаем сразу.
    После исчерпания попыток пробрасываем исключение: день должен упасть громко,
    а не пропасть тихо (иначе в истории будет дыра).
    """
    last_exc: Exception | None = None
    for attempt in range(1, TW_RETRIES + 1):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=timeout)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
        else:
            if resp.status_code < 500 and resp.status_code != 429:
                resp.raise_for_status()
                return resp.json()
            last_exc = requests.exceptions.HTTPError(f"TW HTTP {resp.status_code}", response=resp)
        if attempt < TW_RETRIES:
            sleep_s = TW_BACKOFF_SEC * attempt
            print(f"gcc_tw: транзиентная ошибка {type(last_exc).__name__}, "
                  f"попытка {attempt}/{TW_RETRIES}, повтор через {sleep_s}с")
            time.sleep(sleep_s)
    assert last_exc is not None
    raise last_exc

# metricId (summary-page) → (channel, subchannel); совпадают с map_metrika_channel/map_tw_source.
SPEND_METRIC_MAP = {
    "ga_adCost": ("SEM", "Google.Adwords"),
    "fb_ads_spend": ("SMM paid", "Meta Ads"),
    "totalSnapchatSpend": ("SMM paid", "Snapchat Ads"),
    "totalTiktokSpend": ("SMM paid", "TikTok Ads"),
    "totalPinterestSpend": ("SMM paid", "Pinterest Ads"),
    "totalBingSpend": ("SEM", "Bing"),
}


def order_touchpoint(order: dict) -> dict:
    """Тачпоинт, которым атрибутирован заказ: первый непустой из моделей по приоритету.

    Источник и кампания обязаны браться из ОДНОГО тачпоинта, иначе разъедутся по разным
    моделям атрибуции. Зонд P4: ключей `lastClick`/`firstClick` в ответе нет вовсе,
    цепочка фактически всегда останавливается на `lastPlatformClick`.

    Returns:
        Тачпоинт {source, campaignId, adsetId, adId, clickDate} либо {} если атрибуции нет.
    """
    attribution = order.get("attribution") or {}
    for model in ("lastPlatformClick", "lastClick", "fullLastClick"):
        touchpoints = attribution.get(model) or []
        if touchpoints and isinstance(touchpoints[0], dict) and touchpoints[0].get("source"):
            return touchpoints[0]
    return {}


def order_source(order: dict) -> str | None:
    """Источник заказа: первый непустой source из lastPlatformClick → lastClick → fullLastClick.

    Args:
        order: элемент `ordersWithJourneys` (ключ `attribution` с моделями-списками тачпоинтов).

    Returns:
        source первого тачпоинта найденной модели, либо None если ни в одной модели нет данных.
    """
    attribution = order.get("attribution") or {}
    for model in ("lastPlatformClick", "lastClick", "fullLastClick"):
        touchpoints = attribution.get(model) or []
        if touchpoints and touchpoints[0].get("source"):
            return touchpoints[0]["source"]
    return None


def order_campaign(order: dict) -> str | None:
    """Кампания площадки, которой TW приписал заказ.

    Берём из того же тачпоинта, что и order_source, — иначе источник и кампания
    разъедутся по разным моделям атрибуции. В `journey` меток нет вовсе (TW чистит
    query-параметры: 0 utm на 28 843 тачпоинтах живых данных), поэтому attribution —
    единственный путь. id совпадает с utm_campaign в Метрике и с campaign_id кабинета.

    Args:
        order: элемент `ordersWithJourneys`.

    Returns:
        id кампании либо None (органика/директ/CRM кампаний не имеют).
    """
    attribution = order.get("attribution") or {}
    for model in ("lastPlatformClick", "lastClick", "fullLastClick"):
        touchpoints = attribution.get(model) or []
        if touchpoints and touchpoints[0].get("source"):
            return touchpoints[0].get("campaignId") or None
    return None


def order_country(order: dict) -> str | None:
    """Страна Залива, в которой оформлен заказ — по домену витрины из journey.

    `journey` (доступен только при `excludeJourneyData: false`) отсортирован по убыванию
    времени, поэтому берём первый тачпоинт с распознаваемым доменом — самый близкий к
    моменту заказа. События `add2c` не несут `path` и пропускаются. Правило и его цена
    (расхождение с «доминирующим доменом» — 2 заказа из 84) — зонд P3, docs/GCC_CONTRACTS.md.

    Args:
        order: элемент `ordersWithJourneys`.

    Returns:
        Название страны или None (нет journey / только не-GCC домены) — тогда заказ
        попадает лишь в GCC-тотал.
    """
    for touchpoint in order.get("journey") or []:
        match = _HOST_RE.match(touchpoint.get("path") or "")
        if not match:
            continue
        country = map_domain_country(match.group(1))
        if country:
            return country
    return None


def aggregate_orders_by_channel(orders: list[dict], date: str) -> list[dict]:
    """Свернуть заказы attribution-эндпоинта в строки заказы/выручка по каналу и стране.

    Args:
        orders: список `ordersWithJourneys`.
        date: дата синка (сутки), проставляется во все строки — НЕ берётся из created_at заказов.

    Returns:
        Список дектов {date, country, campaign, channel, subchannel, traffic_type,
        orders, revenue}.
        country=None (заказ без journey) — отдельная строка, суммируется в GCC-тотал.
    """
    agg: dict[tuple[str | None, str | None, str, str, str], dict] = {}
    for order in orders:
        touchpoint = order_touchpoint(order)
        src = touchpoint.get("source") or None
        # У organic_and_social в campaignId лежит домен-реферер, а НЕ id кампании
        # (зонд P4). Он нужен маппингу, чтобы расщепить органику и соцсети, но в
        # колонку кампании его класть нельзя — иначе «yandex.ru» станет кампанией.
        raw_campaign = (touchpoint.get("campaignId") or "").strip() or None
        is_referrer = (src or "").lower() == "organic_and_social"
        channel, subchannel, traffic_type = map_tw_source(src, raw_campaign)
        country = order_country(order)
        campaign = None if is_referrer else raw_campaign
        key = (country, campaign, channel, subchannel, traffic_type)
        row = agg.setdefault(
            key,
            {
                "date": date,
                "country": country,
                "campaign": campaign,
                "channel": channel,
                "subchannel": subchannel,
                "traffic_type": traffic_type,
                "orders": 0,
                "revenue": 0.0,
            },
        )
        row["orders"] += 1
        row["revenue"] += float(order.get("total_price") or 0)
    return list(agg.values())


def fetch_tw_orders(api_key: str, shop: str, date_from: str, date_to: str) -> list[dict]:
    """Получить все заказы с журналами атрибуции за период (с пагинацией TW).

    Args:
        api_key: ключ Triple Whale (x-api-key).
        shop: *.myshopify.com магазина.
        date_from: дата от (YYYY-MM-DD).
        date_to: дата до (YYYY-MM-DD).

    Returns:
        Объединённый список `ordersWithJourneys` со всех страниц.
    """
    headers = {"x-api-key": api_key, "content-type": "application/json"}
    orders: list[dict] = []
    end_date = date_to
    while True:
        # journey нужен для страны заказа (order_country). Ответ раздувается
        # (84 заказа ≈ 29k тачпоинтов) — не логировать заказы целиком.
        body = {
            "shop": shop,
            "startDate": date_from,
            "endDate": end_date,
            "excludeJourneyData": False,
        }
        data = _tw_post(TW_ORDERS_URL, headers, body, timeout=180)
        orders.extend(data.get("ordersWithJourneys") or [])
        earliest_date = data.get("earliestDate")
        if data.get("totalForRange") == data.get("count") or not earliest_date:
            break
        end_date = earliest_date
    return orders


def fetch_tw_spend(api_key: str, shop: str, day: str) -> dict[str, float]:
    """Получить метрики расхода/продаж summary-page за один день.

    Args:
        api_key: ключ Triple Whale (x-api-key).
        shop: *.myshopify.com магазина.
        day: дата (YYYY-MM-DD), start и end периода совпадают.

    Returns:
        {metricId: values.current} по всем метрикам ответа.
    """
    headers = {"x-api-key": api_key, "content-type": "application/json"}
    body = {"shopDomain": shop, "period": {"start": day, "end": day}, "todayHour": 25}
    data = _tw_post(TW_SPEND_URL, headers, body, timeout=120)
    return {
        m["metricId"]: (m.get("values") or {}).get("current")
        for m in data.get("metrics") or []
        if m.get("metricId")
    }


def spend_by_channel(spend: dict[str, float], date: str) -> list[dict]:
    """Расход по каналам из метрик summary-page (только платные, cost>0).

    Args:
        spend: {metricId: current} — результат fetch_tw_spend (или собранный вручную из фикстуры).
        date: дата синка (сутки).

    Returns:
        Список дектов {date, channel, subchannel, traffic_type="Платный", cost}.
    """
    rows = []
    for metric_id, (channel, subchannel) in SPEND_METRIC_MAP.items():
        cost = spend.get(metric_id)
        if not cost:
            continue
        rows.append(
            {
                "date": date,
                "channel": channel,
                "subchannel": subchannel,
                "traffic_type": "Платный",
                "cost": float(cost),
            }
        )
    return rows
