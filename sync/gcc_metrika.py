"""Функции для работы с Яндекс.Метрика Stat API (счётчик 98232701, трафик GCC)."""

import requests


def parse_metrika_traffic(resp: dict) -> list[dict]:
    """Разбор ответа Metrika Stat API в список строк трафика.

    Args:
        resp: полный ответ API с ключами "data", "totals" и т.д.

    Returns:
        Список дектов с ключами:
        - date (str): YYYY-MM-DD из dimensions[0].name
        - traffic_source (str|None): из dimensions[1].id
        - source_engine (str|None): из dimensions[2].name
        - visits (float): из metrics[0]
        - users (float): из metrics[1]
    """
    rows = []
    for item in resp.get("data", []):
        dims = item.get("dimensions", [])
        metrics = item.get("metrics", [])

        # Извлекаем dimensions
        date = dims[0].get("name") if len(dims) > 0 else None
        traffic_source = dims[1].get("id") if len(dims) > 1 else None
        source_engine = dims[2].get("name") if len(dims) > 2 else None

        # Извлекаем metrics
        visits = metrics[0] if len(metrics) > 0 else None
        users = metrics[1] if len(metrics) > 1 else None

        rows.append(
            {
                "date": date,
                "traffic_source": traffic_source,
                "source_engine": source_engine,
                "visits": visits,
                "users": users,
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
        "dimensions": "ym:s:date,ym:s:lastsignTrafficSource,ym:s:lastsignSourceEngine",
        "accuracy": "full",
        "limit": 100000,
    }
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return parse_metrika_traffic(resp.json())
