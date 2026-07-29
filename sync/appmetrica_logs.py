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
# event_json нужен ради суммы заказа и transaction_id (в нём же лежит корзина —
# она не используется, но выбросить её на стороне API нельзя, поэтому события
# тянутся помесячными чанками, см. sync_lime_appmetrica).
EVENT_FIELDS = "appmetrica_device_id,event_name,event_datetime,event_json"


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


# Гео для GCC-разреза (по стране). AppMetrica отдаёт ISO-код страны события/установки.
_GEO = ",city,country_iso_code"

# Сессии (app-трафик = sessions, не installs). У сессии нет publisher (источник —
# атрибут установки), поэтому paid/organic сессий берётся джойном device → install.
SESSION_FIELDS = "appmetrica_device_id,session_start_datetime"


def fetch_installations(app_id: str, token: str, date_since: str, date_until: str,
                        country: bool = False) -> list[dict]:
    params = {
        "application_id": app_id,
        "date_since": f"{date_since} 00:00:00",
        "date_until": f"{date_until} 23:59:59",
        "date_dimension": "default",  # время события установки
        "fields": INSTALL_FIELDS + (_GEO if country else ""),
    }
    return _export("installations", params, token)


def fetch_purchase_events(app_id: str, token: str, date_since: str, date_until: str,
                          event_name: str, country: bool = False) -> list[dict]:
    params = {
        "application_id": app_id,
        "date_since": f"{date_since} 00:00:00",
        "date_until": f"{date_until} 23:59:59",
        # 'receive' (время приёма сервером), а не 'default': откалибровано по эталону
        # AppMetrica UI (янв-2026, M0). С 'default' покупки завышались на +0.5..0.8%
        # из-за сдвига границы месяца; с 'receive' VK совпадает точно (206), остальные
        # в пределах 0.3-0.4% (остаток — антифрод-фильтрация отчётов, в сыром логе её нет).
        "date_dimension": "receive",
        "fields": EVENT_FIELDS + (_GEO if country else ""),
        "event_name": event_name,  # серверный фильтр по имени события
    }
    return _export("events", params, token)


def fetch_sessions(app_id: str, token: str, date_since: str, date_until: str,
                   country: bool = False) -> list[dict]:
    """Старты сессий (app-трафик). date_dimension=default — время старта сессии."""
    params = {
        "application_id": app_id,
        "date_since": f"{date_since} 00:00:00",
        "date_until": f"{date_until} 23:59:59",
        "date_dimension": "default",
        "fields": SESSION_FIELDS + (_GEO if country else ""),
    }
    return _export("sessions_starts", params, token)


def fetch_export(endpoint: str, app_id: str, token: str, date_since: str, date_until: str,
                 fields: str, date_dimension: str = "default") -> list[dict]:
    """Сырой вызов произвольного endpoint Logs API — для разведки (deeplinks и пр.)."""
    params = {
        "application_id": app_id,
        "date_since": f"{date_since} 00:00:00",
        "date_until": f"{date_until} 23:59:59",
        "date_dimension": date_dimension,
        "fields": fields,
    }
    return _export(endpoint, params, token)
