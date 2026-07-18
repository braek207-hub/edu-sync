"""Функции для работы с Яндекс.Метрика Stat API (счётчик 98232701, трафик GCC)."""

import requests

from sync.gcc_channels import map_domain_country

# Прод-набор измерений. startURLDomain даёт страну Залива (зонд P1: делит трафик на все 6
# стран, сумма по доменам = totals; ym:s:URLDomain не существует — HTTP 400).
DIMENSIONS = (
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


def fetch_metrika_traffic(
    counter_id: int, token: str, date_from: str, date_to: str
) -> list[dict]:
    """Получить трафик из Яндекс.Метрики и распарсить.

    Args:
        counter_id: ID счётчика (напр. 98232701)
        token: OAuth token для API
        date_from: дата от в формате YYYY-MM-DD
        date_to: дата до в формате YYYY-MM-DD

    Returns:
        Результат parse_metrika_traffic()
    """
    url = "https://api-metrika.yandex.net/stat/v1/data"
    headers = {"Authorization": f"OAuth {token}"}
    params = {
        "ids": counter_id,
        "date1": date_from,
        "date2": date_to,
        "metrics": "ym:s:visits,ym:s:users,ym:s:pageviews,ym:s:bounceRate",
        "dimensions": ",".join(DIMENSIONS),
        "accuracy": "full",
        "limit": 100000,
    }
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return parse_metrika_traffic(resp.json())
