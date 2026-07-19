"""Функции для работы с Яндекс.Метрика Stat API (счётчик 98232701, трафик GCC)."""

import os

import requests

from sync.gcc_channels import map_domain_country

# Измерения канала (эталон тотала: без домена и без разбивок).
CHANNEL_DIMENSIONS = (
    "ym:s:date",
    "ym:s:lastsignTrafficSource",
)

# Набор для НЕрекламного трафика. ⚠️ Движка источника здесь нет намеренно:
# `lastsignSourceEngine` в кроссе с доменом выбрасывает мелкие комбинации вместо
# схлопывания в «прочее» — Бахрейн терял 505 визитов и 7 источников → 299 и один Internal
# (зонд 2026-07-18). utm-метки и searchEngine такого не делают: потерь нет.
COUNTRY_DIMENSIONS = (
    "ym:s:date",
    "ym:s:startURLDomain",
    "ym:s:lastsignTrafficSource",
    "ym:s:UTMSource",
    "ym:s:UTMCampaign",
    "ym:s:lastsignSearchEngine",
)

# Реклама — отдельным запросом С движком. Зонд П4 (2026-07-19, docs/GCC_CONTRACTS.md):
# обрезка бьёт по ХВОСТУ, поэтому на узкой выборке «только реклама» она почти не работает —
# движок стоит 0.44% против 42% на общем запросе, а площадка известна у 100% платных
# визитов (Google Ads 49%, Instagram 37%, Facebook 14%; корзины «без движка» нет).
# До этого площадка бралась из utm, и 23% платного трафика висело подканалом «Ad».
AD_FILTER = "ym:s:lastsignTrafficSource=='ad'"
# Соцсети — тоже своим запросом с движком: без него сеть неизвестна и визиты идут
# «SMM (organic)/Others», не встречаясь с заказами TW, где сеть названа (зонд 2026-07-19:
# 3 766 визитов, из них instagram.com 71.9%, Facebook 28.1%). Обрезка тут заметнее, чем
# у рекламы (−7.43% против −0.44%: хвост меньше), поэтому разницу добираем остатком.
SOCIAL_FILTER = "ym:s:lastsignTrafficSource=='social'"
NONAD_FILTER = ("ym:s:lastsignTrafficSource!='ad' "
                "AND ym:s:lastsignTrafficSource!='social'")

# Площадка + кампания: стоит 3.13%, разницу добираем строкой-остатком с известной площадкой.
AD_DIMENSIONS = (
    "ym:s:date",
    "ym:s:startURLDomain",
    "ym:s:lastsignTrafficSource",
    "ym:s:lastsignSourceEngine",
    "ym:s:UTMCampaign",
)

# Та же грань без кампании (0.44%) — эталон для остатка внутри рекламы.
# Она же используется для соцсетей: кампаний у них нет, нужна только сеть.
AD_ENGINE_DIMENSIONS = (
    "ym:s:date",
    "ym:s:startURLDomain",
    "ym:s:lastsignTrafficSource",
    "ym:s:lastsignSourceEngine",
)

# Цели счётчика GCC (проверено 2026-07-18 на живых данных за неделю):
#   344184922 «Ecommerce: добавление в корзину» — 7955 достижений, работает;
#   344184921 «Автоцель: просмотр корзины» — 2446, взята как шаг «оформление»:
#   штатная автоцель 367696661 «начало оформления заказа» на этом счётчике не
#   срабатывает (0 достижений), «возврат из платёжной системы» тоже 0.
# Переопределяются через env, если Павел заведёт настоящую цель чекаута.
GOAL_CART = os.environ.get("GCC_METRICA_GOAL_CART") or "344184922"
GOAL_CHECKOUT = os.environ.get("GCC_METRICA_GOAL_CHECKOUT") or "344184921"

# Порядок метрик в запросе = порядок чтения в parse_metrika_traffic.
METRICS = (
    "ym:s:visits",
    "ym:s:users",
    "ym:s:newUsers",
    "ym:s:bounceRate",
    "ym:s:pageDepth",
    f"ym:s:goal{GOAL_CART}reaches",
    f"ym:s:goal{GOAL_CHECKOUT}reaches",
)

# utm_source → название площадки в терминах map_metrika_channel.
_UTM_SOURCE_ENGINE = {
    "google": "Google Ads",
    "googleads": "Google Ads",
    "google-ads": "Google Ads",
    "adwords": "Google Ads",
    "ig": "Instagram",
    "instagram": "Instagram",
    "facebook": "Facebook",
    "fb": "Facebook",
    "meta": "Facebook",
    "tiktok": "TikTok",
    "snapchat": "Snapchat",
    "yandex": "Yandex.Direct",
}


def resolve_engine(traffic_source, utm_source, campaign, search_engine):
    """Восстановить площадку («движок») источника без режущего измерения Метрики.

    Args:
        traffic_source: id источника ("ad", "organic", "social", ...).
        utm_source: метка utm_source визита.
        campaign: метка utm_campaign (у Google Ads это id кампании через ValueTrack).
        search_engine: `ym:s:lastsignSearchEngine` — поисковик, не режет выдачу.

    Returns:
        Название площадки для map_metrika_channel либо None — тогда подканал
        останется generic, но визит не потеряется (у малых стран реклама часто без меток).
    """
    src = (utm_source or "").strip().lower()
    if src in _UTM_SOURCE_ENGINE:
        return _UTM_SOURCE_ENGINE[src]

    camp = (campaign or "").strip()
    if camp:
        low = camp.lower()
        if any(x in low for x in ("instagram", "facebook", "_fb", "meta")):
            return "Instagram"
        if camp.isdigit():
            # Google Ads пишет id кампании (10-12 цифр), у Meta id заметно длиннее (17-19).
            if 10 <= len(camp) <= 12:
                return "Google Ads"
            if len(camp) >= 15:
                return "Instagram"

    if (traffic_source or "") in ("organic", "search") and search_engine:
        return search_engine

    return None


def parse_metrika_traffic(resp: dict) -> list[dict]:
    """Разбор ответа Metrika Stat API в список строк трафика.

    Позиции измерений читаются из `resp["query"]["dimensions"]` (API возвращает эхо запроса),
    поэтому добавление/удаление измерения не ломает разбор.

    Args:
        resp: полный ответ API с ключами "query", "data", "totals" и т.д.

    Returns:
        Список дектов с ключами:
        - date (str): YYYY-MM-DD
        - country (str|None): страна Залива по домену витрины (None вне GCC)
        - campaign (str|None): utm_campaign (у Google Ads это id кампании)
        - traffic_source (str|None): id источника (напр. "ad", "organic")
        - source_engine (str|None): площадка, восстановленная из utm (см. resolve_engine)
        - visits, users, new_users (float)
        - bounce_w, depth_w (float): отказы/глубина, взвешенные на визиты (аддитивны)
        - cart_reaches, checkout_reaches (float): достижения целей корзины/оформления
    """
    queried = (resp.get("query") or {}).get("dimensions") or []
    pos = {name: i for i, name in enumerate(queried)}

    def dim(dims: list, attr: str, field: str):
        i = pos.get(attr)
        return dims[i].get(field) if i is not None and i < len(dims) else None

    rows = []
    for item in resp.get("data", []):
        dims = item.get("dimensions", [])
        metrics = item.get("metrics", [])

        traffic_source = dim(dims, "ym:s:lastsignTrafficSource", "id")
        campaign = dim(dims, "ym:s:UTMCampaign", "name") or None
        utm_source = dim(dims, "ym:s:UTMSource", "name") or None
        search_engine = dim(dims, "ym:s:lastsignSearchEngine", "name") or None
        visits = metrics[0] if len(metrics) > 0 else None

        # Настоящий движок есть только в рекламном запросе. Где он есть — берём его,
        # он точнее восстановленного из utm и покрывает визиты вовсе без меток.
        real_engine = dim(dims, "ym:s:lastsignSourceEngine", "name") or None
        engine = real_engine or resolve_engine(
            traffic_source, utm_source, campaign, search_engine
        )

        rows.append(
            {
                "date": dim(dims, "ym:s:date", "name"),
                "country": map_domain_country(dim(dims, "ym:s:startURLDomain", "name")),
                "campaign": campaign,
                "traffic_source": traffic_source,
                # Нужен маппингу, чтобы отличить рассылку Mindbox от прочей почты.
                "utm_source": utm_source,
                "source_engine": engine,
                "visits": visits,
                "users": metrics[1] if len(metrics) > 1 else None,
                "new_users": metrics[2] if len(metrics) > 2 else None,
                # Отказы и глубина — средние по строке. Наружу отдаём взвешенными на визиты,
                # иначе при суммировании строк среднее от средних соврёт.
                "bounce_w": (metrics[3] or 0) / 100 * (visits or 0) if len(metrics) > 3 else None,
                "depth_w": (metrics[4] or 0) * (visits or 0) if len(metrics) > 4 else None,
                "cart_reaches": metrics[5] if len(metrics) > 5 else None,
                "checkout_reaches": metrics[6] if len(metrics) > 6 else None,
            }
        )
    return rows


def residual_rows(total_rows: list[dict], country_rows: list[dict]) -> list[dict]:
    """Визиты канала, которые не попали ни в одну страну → строки с country=None.

    Метрика при кроссе `ym:s:startURLDomain` с lastsignTrafficSource+lastsignSourceEngine
    отдаёт меньше визитов, чем тот же запрос без домена (2026-07-17: 4396 против 4496,
    −2.2%; потеря воспроизводится и при per-domain запросе с фильтром). Без компенсации
    GCC-тотал в дашборде просел бы при переходе на дробление. Разницу по каждому каналу
    пишем отдельной строкой без страны — та же семантика, что у расхода без гео-разбивки.

    Args:
        total_rows: разбор запроса по CHANNEL_DIMENSIONS (country=None у всех строк).
        country_rows: разбор запроса по COUNTRY_DIMENSIONS.

    Returns:
        Строки-остатки в формате parse_metrika_traffic (только там, где остаток > 0).
    """
    def key(row):
        # Только (дата, источник) — ровно то, что запрашивает CHANNEL_DIMENSIONS.
        # Площадка сюда НЕ входит: эталонный запрос её не знает (source_engine=None),
        # а у детальных строк она восстановлена из utm → ключи не совпали бы и остаток
        # продублировал бы весь трафик канала (поймано dry-run: 100 → 2523 визита).
        return (row["date"], row["traffic_source"])

    attributed: dict[tuple, list[int]] = {}
    for row in country_rows:
        acc = attributed.setdefault(key(row), [0, 0])
        acc[0] += int(row["visits"] or 0)
        acc[1] += int(row["users"] or 0)

    out = []
    for row in total_rows:
        visits_seen, users_seen = attributed.get(key(row), (0, 0))
        visits = int(row["visits"] or 0) - visits_seen
        if visits <= 0:
            continue
        out.append({
            "date": row["date"],
            "country": None,
            "traffic_source": row["traffic_source"],
            "source_engine": row["source_engine"],
            "visits": visits,
            "users": max(int(row["users"] or 0) - users_seen, 0),
        })
    return out


def ad_engine_residual(engine_rows: list[dict], detail_rows: list[dict]) -> list[dict]:
    """Платные визиты, не попавшие в разрез с кампанией → строки с площадкой, без кампании.

    Кампания стоит 3.13% визитов (зонд П4), и теряются они на хвосте — то есть на мелких
    странах. Разницу по (дата, страна, площадка) дописываем отдельной строкой: площадка
    сохраняется, теряется только кампания. Без этого платный тотал просел бы на 3%.
    """
    def key(row):
        return (row["date"], row["country"], row["source_engine"])

    # Вычитать надо ВСЕ аддитивные метрики, а не только визиты: bounce_w/depth_w взвешены
    # на визиты, и если оставить их от строки-эталона целиком, они сложатся с детальными
    # и отказы с глубиной задвоятся почти вдвое (знаменатель-то останется прежним).
    ADDITIVE = ("visits", "users", "new_users", "bounce_w", "depth_w",
                "cart_reaches", "checkout_reaches")

    attributed: dict[tuple, dict[str, float]] = {}
    for row in detail_rows:
        acc = attributed.setdefault(key(row), {m: 0.0 for m in ADDITIVE})
        for metric in ADDITIVE:
            acc[metric] += float(row.get(metric) or 0)

    out = []
    for row in engine_rows:
        seen = attributed.get(key(row), {})
        visits = float(row.get("visits") or 0) - float(seen.get("visits") or 0)
        if visits <= 0:
            continue
        residual = {**row, "campaign": None}
        for metric in ADDITIVE:
            residual[metric] = max(
                float(row.get(metric) or 0) - float(seen.get(metric) or 0), 0.0
            )
        out.append(residual)
    return out


def _fetch(counter_id, token: str, date_from: str, date_to: str, dimensions,
           filters: str | None = None) -> list[dict]:
    params = {
        "ids": counter_id,
        "date1": date_from,
        "date2": date_to,
        "metrics": ",".join(METRICS),
        "dimensions": ",".join(dimensions),
        "accuracy": "full",
        "limit": 100000,
    }
    if filters:
        params["filters"] = filters
    resp = requests.get(
        "https://api-metrika.yandex.net/stat/v1/data",
        headers={"Authorization": f"OAuth {token}"},
        params=params,
        timeout=60,
    )
    resp.raise_for_status()
    return parse_metrika_traffic(resp.json())


def fetch_metrika_traffic(
    counter_id: int, token: str, date_from: str, date_to: str
) -> list[dict]:
    """Получить трафик из Яндекс.Метрики: строки по странам + остаток до полного тотала.

    Два запроса: с доменом (страны) и без (эталонный тотал по каналам). Разница по каналу
    добирается строкой country=None — см. residual_rows.

    Args:
        counter_id: ID счётчика (напр. 98232701)
        token: OAuth token для API
        date_from: дата от в формате YYYY-MM-DD
        date_to: дата до в формате YYYY-MM-DD

    Returns:
        Строки parse_metrika_traffic: по странам + остатки (country=None).
    """
    # Прочий трафик — прежним набором (движок бы его порезал).
    nonad = _fetch(counter_id, token, date_from, date_to, COUNTRY_DIMENSIONS, NONAD_FILTER)

    # Реклама — своим запросом С движком: на узкой выборке обрезка почти не работает,
    # и площадка известна даже у визитов вовсе без utm-меток (зонд П4).
    ad_detail = _fetch(counter_id, token, date_from, date_to, AD_DIMENSIONS, AD_FILTER)
    ad_engine = _fetch(counter_id, token, date_from, date_to, AD_ENGINE_DIMENSIONS, AD_FILTER)
    ad = ad_detail + ad_engine_residual(ad_engine, ad_detail)

    # Соцсети — тем же приёмом: сеть нужна, чтобы визиты встретились с заказами TW.
    # Эталон здесь без домена: обрезка (−7.43%) съедает именно страновой разрез.
    social_detail = _fetch(counter_id, token, date_from, date_to,
                           AD_ENGINE_DIMENSIONS, SOCIAL_FILTER)
    social_total = _fetch(counter_id, token, date_from, date_to,
                          CHANNEL_DIMENSIONS, SOCIAL_FILTER)
    social = social_detail + residual_rows(social_total, social_detail)

    by_country = nonad + ad + social
    totals = _fetch(counter_id, token, date_from, date_to, CHANNEL_DIMENSIONS)
    return by_country + residual_rows(totals, by_country)
