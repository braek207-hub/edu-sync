# -*- coding: utf-8 -*-
"""
sync/direct_settings_cabinets.py — синк настроек кампаний Директа для кабинетов
не-EDU дашбордов (meshnflesh, bjorn, polinarepik) → <x>_campaign_settings
и дневной снимок в strategy_snapshots (журнал изменений Panda-BI).

Весь разбор настроек — общий с EDU (sync/edu_direct_settings.py, параметры
table/extra_counter_ids): копировать полторы тысячи строк под каждый кабинет — плодить
расхождения. Здесь только «какой кабинет чем открывать»: токен, логины, счётчики.

Таблицы bjorn_campaign_settings / polinarepik_campaign_settings создаёт миграция
Panda-BI 20261007120000_change_journal_all_dashboards. До неё INSERT упадёт с
UndefinedTable — и это ДОЛЖНО уронить джоб: тихий пропуск скрыл бы отсутствие данных
под зелёной галочкой.

Запуск:  python -m sync.direct_settings_cabinets <meshnflesh|bjorn|polinarepik>

ENV (секреты GitHub — по кабинету):
    DATABASE_URL
    meshnflesh:  MESHNFLESH_DIRECT_TOKEN, MESHNFLESH_YANDEX_TOKEN (имена целей, опц.)
    bjorn:       BJORN_DIRECT_CLIENTS_JSON ([{login, token}] — тот же, что у sync-bjorn),
                 BJORN_METRICA_TOKEN, BJORN_METRICA_COUNTER_ID (имена целей, опц.)
    polinarepik: POLINAREPIK_YANDEX_TOKEN (им же синк polinarepik.py ходит в Директ),
                 POLINAREPIK_DIRECT_CLIENT_LOGIN (опц., по умолчанию polinarepik-wear)
"""

import json
import os
import sys
import traceback
from dataclasses import dataclass, field
from typing import Callable, Dict, List

import sync.edu_direct_settings as eds
from sync.strategy_snapshots import write_snapshot


@dataclass(frozen=True)
class Cabinet:
    dashboard: str
    table: str
    # [{login, token}] — у BJORN логинов может быть несколько (как в sync/bjorn).
    clients: Callable[[], List[Dict[str, str]]]
    counters: Callable[[], List[int]] = field(default=lambda: [])
    # env с токеном Метрики для имён целей; пусто — goalNames останутся пустыми.
    ym_token_env: str = ""


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _required(name: str) -> str:
    v = _env(name)
    if not v:
        raise RuntimeError(f"{name} не задан")
    return v


def parse_clients_json(raw: str, default_token: str = "") -> List[Dict[str, str]]:
    """Формат BJORN_DIRECT_CLIENTS_JSON — тот же, что читает sync/bjorn/sync_direct.py:
    массив строк-логинов или объектов {login|client_login, token}."""
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise RuntimeError("CLIENTS_JSON должен быть JSON-массивом")
    out: List[Dict[str, str]] = []
    for item in parsed:
        if isinstance(item, str) and item.strip():
            out.append({"login": item.strip(), "token": default_token})
        elif isinstance(item, dict):
            login = str(item.get("login") or item.get("client_login") or "").strip()
            token = str(item.get("token") or "").strip() or default_token
            if login:
                out.append({"login": login, "token": token})
    if not out:
        raise RuntimeError("CLIENTS_JSON не содержит ни одного логина")
    return out


def _mnf_clients() -> List[Dict[str, str]]:
    return [{"login": "meshnflesh", "token": _required("MESHNFLESH_DIRECT_TOKEN")}]


def _bjorn_clients() -> List[Dict[str, str]]:
    return parse_clients_json(_required("BJORN_DIRECT_CLIENTS_JSON"), _env("BJORN_DIRECT_TOKEN"))


def _polina_clients() -> List[Dict[str, str]]:
    token = _env("POLINAREPIK_DIRECT_TOKEN") or _required("POLINAREPIK_YANDEX_TOKEN")
    login = _env("POLINAREPIK_DIRECT_CLIENT_LOGIN") or "polinarepik-wear"
    return [{"login": login, "token": token}]


def _bjorn_counters() -> List[int]:
    raw = _env("BJORN_METRICA_COUNTER_ID")
    return [int(raw)] if raw.isdigit() else []


CABINETS: Dict[str, Cabinet] = {
    # Счётчик Метрики meshnflesh (см. sync/meshnflesh_metrika.py).
    "meshnflesh": Cabinet(
        "meshnflesh", "mnf_campaign_settings", _mnf_clients,
        counters=lambda: [82116769], ym_token_env="MESHNFLESH_YANDEX_TOKEN",
    ),
    "bjorn": Cabinet(
        "bjorn", "bjorn_campaign_settings", _bjorn_clients,
        counters=_bjorn_counters, ym_token_env="BJORN_METRICA_TOKEN",
    ),
    # Счётчик Polina — sync/polinarepik.py METRICA_COUNTER_ID.
    "polinarepik": Cabinet(
        "polinarepik", "polinarepik_campaign_settings", _polina_clients,
        counters=lambda: [100764399], ym_token_env="POLINAREPIK_YANDEX_TOKEN",
    ),
}


def sync_cabinet(key: str) -> int:
    """Настройки всех логинов кабинета → витрина, затем снимок дня.

    Общий модуль edu_direct_settings читает токен из env DIRECT_TOKEN и логин из
    глобального _CURRENT_LOGIN (тот же приём, что sync_edu_campaign_settings для
    логинов EDU). Каждый кабинет живёт своим шагом/джобом Actions — подмена
    os.environ здесь никого не задевает.
    """
    cab = CABINETS[key]
    ym = _env(cab.ym_token_env) if cab.ym_token_env else ""
    if ym:
        os.environ["YM_TOKEN"] = ym
    else:
        os.environ.pop("YM_TOKEN", None)

    total = 0
    failed: List[str] = []
    for client in cab.clients():
        login = client["login"]
        if not client.get("token"):
            failed.append(login)
            print(f"[direct_settings:{key}] {login}: ОШИБКА нет токена")
            continue
        os.environ["DIRECT_TOKEN"] = client["token"]
        eds._CURRENT_LOGIN = login
        try:
            names = eds._list_campaigns_for_login()
            campaign_ids = sorted(names.keys())
            if not campaign_ids:
                print(f"[direct_settings:{key}] {login}: 0 кампаний")
                continue
            n = eds._sync_campaign_settings(
                campaign_ids, names, table=cab.table, extra_counter_ids=cab.counters()
            )
            print(f"[direct_settings:{key}] {login}: настройки {n} кампаний")
            total += n
        except Exception as e:
            failed.append(login)
            print(f"[direct_settings:{key}] {login}: ОШИБКА настройки не синхронизированы: {e}")
            traceback.print_exc()

    # Снимок — по всей витрине, как у EDU. Упал снимок — красный прогон: журнал
    # изменений без него не пополняется, и это должно быть видно.
    if total:
        write_snapshot(eds._pg_url(), cab.dashboard)
    if failed:
        raise RuntimeError(f"{key}: настройки не синхронизированы для логинов: " + ", ".join(failed))
    return total


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in CABINETS:
        raise SystemExit("usage: python -m sync.direct_settings_cabinets <" + "|".join(CABINETS) + ">")
    sync_cabinet(sys.argv[1])
