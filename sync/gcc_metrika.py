"""Функции для работы с Яндекс.Метрика Stat API (счётчик 98232701, трафик GCC)."""

import requests

from sync.gcc_channels import map_domain_country

# Измерения канала (были прод-набором до дробления по странам) — дают точный GCC-тотал.
CHANNEL_DIMENSIONS = (
    "ym:s:date",
    "ym:s:lastsignTrafficSource",
    "ym:s:lastsignSourceEngine",
)

# Те же + домен витрины → страна Залива (зонд P1; ym:s:URLDomain не существует, HTTP 400).
COUNTRY_DIMENSIONS = (
    "ym:s:date",
    "ym:s:startURLDomain",
    "ym:s:lastsignTrafficSource",
    "ym:s:lastsignSourceEngine",
)


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
        - traffic_source (str|None): id источника (напр. "ad", "organic")
        - source_engine (str|None): название движка (напр. "Google Ads")
        - visits (float), users (float)
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

        rows.append(
            {
                "date": dim(dims, "ym:s:date", "name"),
                "country": map_domain_country(dim(dims, "ym:s:startURLDomain", "name")),
                "traffic_source": dim(dims, "ym:s:lastsignTrafficSource", "id"),
                "source_engine": dim(dims, "ym:s:lastsignSourceEngine", "name"),
                "visits": metrics[0] if len(metrics) > 0 else None,
                "users": metrics[1] if len(metrics) > 1 else None,
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
        return (row["date"], row["traffic_source"], row["source_engine"])

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


def _fetch(counter_id, token: str, date_from: str, date_to: str, dimensions) -> list[dict]:
    resp = requests.get(
        "https://api-metrika.yandex.net/stat/v1/data",
        headers={"Authorization": f"OAuth {token}"},
        params={
            "ids": counter_id,
            "date1": date_from,
            "date2": date_to,
            "metrics": "ym:s:visits,ym:s:users,ym:s:pageviews,ym:s:bounceRate",
            "dimensions": ",".join(dimensions),
            "accuracy": "full",
            "limit": 100000,
        },
        timeout=30,
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
    by_country = _fetch(counter_id, token, date_from, date_to, COUNTRY_DIMENSIONS)
    totals = _fetch(counter_id, token, date_from, date_to, CHANNEL_DIMENSIONS)
    return by_country + residual_rows(totals, by_country)
