"""Triple Whale-клиент GCC: заказы по каналам (attribution) + расход по каналам (summary-page).

Всё в AED — конверт в ₽ делает оркестратор (B4), не этот модуль.
Каналы/подканалы совпадают с sync.gcc_channels.map_metrika_channel — нужно
для мержа трафика (Метрика) с деньгами (Triple Whale) по (channel, subchannel).
"""
import re

import requests

from sync.gcc_channels import map_domain_country, map_tw_source

_HOST_RE = re.compile(r"https?://([^/]+)", re.I)

TW_ORDERS_URL = "https://api.triplewhale.com/api/v2/attribution/get-orders-with-journeys-v2"
TW_SPEND_URL = "https://api.triplewhale.com/api/v2/summary-page/get-data"

# metricId (summary-page) → (channel, subchannel); совпадают с map_metrika_channel/map_tw_source.
SPEND_METRIC_MAP = {
    "ga_adCost": ("SEM", "Google.Adwords"),
    "fb_ads_spend": ("SMM paid", "Meta Ads"),
    "totalSnapchatSpend": ("SMM paid", "Snapchat Ads"),
    "totalTiktokSpend": ("SMM paid", "TikTok Ads"),
    "totalPinterestSpend": ("SMM paid", "Pinterest Ads"),
    "totalBingSpend": ("SEM", "Bing"),
}


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
        Список дектов {date, country, channel, subchannel, traffic_type, orders, revenue}.
        country=None (заказ без journey) — отдельная строка, суммируется в GCC-тотал.
    """
    agg: dict[tuple[str | None, str, str, str], dict] = {}
    for order in orders:
        src = order_source(order)
        channel, subchannel, traffic_type = map_tw_source(src)
        country = order_country(order)
        key = (country, channel, subchannel, traffic_type)
        row = agg.setdefault(
            key,
            {
                "date": date,
                "country": country,
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
        resp = requests.post(TW_ORDERS_URL, headers=headers, json=body, timeout=90)
        resp.raise_for_status()
        data = resp.json()
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
    resp = requests.post(TW_SPEND_URL, headers=headers, json=body, timeout=60)
    resp.raise_for_status()
    data = resp.json()
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
