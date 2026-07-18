"""Клиент AppMetrica Logs API: сырьё installations + events (purchase).

Logs API асинхронный: первый запрос ставит подготовку файла (HTTP 202),
повторные — поллинг до готовности (HTTP 200 с телом {"data": [...]}).
Даты — datetime 'YYYY-MM-DD HH:MM:SS'. Авторизация — заголовок OAuth-токеном.
"""
import time

import requests

BASE = "https://api.appmetrica.yandex.ru/logs/v1/export"
POLL_INTERVAL_SEC = 20
POLL_MAX_ATTEMPTS = 60  # до ~20 минут ожидания подготовки

INSTALL_FIELDS = (
    "appmetrica_device_id,install_datetime,publisher_name,"
    # click_url_parameters — параметры ссылки трекера; из них берём utm_source
    # (детализация партнёра: с какого трафика человек пришёл перед установкой).
    "click_url_parameters,"
    "is_reattribution,is_reinstallation"
)
EVENT_FIELDS = "appmetrica_device_id,event_name,event_datetime"


def _export(endpoint: str, params: dict, token: str) -> list[dict]:
    url = f"{BASE}/{endpoint}.json"
    headers = {"Authorization": f"OAuth {token}"}
    for _ in range(POLL_MAX_ATTEMPTS):
        r = requests.get(url, params=params, headers=headers, timeout=120)
        if r.status_code == 200:
            return r.json().get("data", [])
        if r.status_code == 202:
            time.sleep(POLL_INTERVAL_SEC)
            continue
        raise RuntimeError(f"Logs API {endpoint} HTTP {r.status_code}: {r.text[:300]}")
    raise TimeoutError(f"Logs API {endpoint}: файл не готов за отведённое время")


def fetch_installations(app_id: str, token: str, date_since: str, date_until: str) -> list[dict]:
    params = {
        "application_id": app_id,
        "date_since": f"{date_since} 00:00:00",
        "date_until": f"{date_until} 23:59:59",
        "date_dimension": "default",  # время события установки
        "fields": INSTALL_FIELDS,
    }
    return _export("installations", params, token)


def fetch_purchase_events(app_id: str, token: str, date_since: str, date_until: str,
                          event_name: str) -> list[dict]:
    params = {
        "application_id": app_id,
        "date_since": f"{date_since} 00:00:00",
        "date_until": f"{date_until} 23:59:59",
        # 'receive' (время приёма сервером), а не 'default': откалибровано по эталону
        # AppMetrica UI (янв-2026, M0). С 'default' покупки завышались на +0.5..0.8%
        # из-за сдвига границы месяца; с 'receive' VK совпадает точно (206), остальные
        # в пределах 0.3-0.4% (остаток — антифрод-фильтрация отчётов, в сыром логе её нет).
        "date_dimension": "receive",
        "fields": EVENT_FIELDS,
        "event_name": event_name,  # серверный фильтр по имени события
    }
    return _export("events", params, token)
