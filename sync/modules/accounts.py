# -*- coding: utf-8 -*-
"""Кабинеты Директа проекта Panda-BI для модулей.

Имена переменных — те, что уже живут в проде (sync/direct_settings_cabinets.py);
свои названия тут не заводим, иначе кабинет молча пропадает из прогона.
Окружение читается только внутри вызова: тесты подают словарь.
"""

import os
from dataclasses import dataclass
from typing import Callable, List, Mapping, Optional, Tuple

from sync.direct_settings_cabinets import parse_clients_json


@dataclass(frozen=True)
class DirectAccount:
    login: str
    token: str
    currency: str = "RUB"
    label: str = ""


def agency_logins_by_token(token: str) -> List[str]:
    """Клиенты агентства прямо из Директа. Ошибку не глотаем: решает вызывающий."""
    from sync.placements.direct import call

    out: List[str] = []
    offset = 0
    while True:
        res = call(token, "", "agencyclients", {
            "method": "get",
            "params": {"SelectionCriteria": {},
                       "FieldNames": ["Login"],
                       "Page": {"Limit": 1000, "Offset": offset}},
        })
        chunk = res.get("Clients", [])
        out.extend(str(c.get("Login", "")).strip() for c in chunk
                   if str(c.get("Login", "")).strip())
        if len(chunk) < 1000:
            return out
        offset += len(chunk)


def _get(env: Mapping[str, str], name: str) -> str:
    return str(env.get(name, "") or "").strip()


def _json_clients(env, name: str, default_token: str, why: List[str]):
    raw = _get(env, name)
    if not raw:
        return []
    try:
        return parse_clients_json(raw, default_token)
    except (ValueError, RuntimeError) as err:
        why.append("%s: не разобрать (%s)" % (name, err))
        return []


def _edu(env, agency_logins, why) -> List[DirectAccount]:
    token = _get(env, "DIRECT_TOKEN")
    if not token:
        why.append("нет токена в DIRECT_TOKEN")
        return []
    logins: List[str] = []
    try:
        logins = [l for l in agency_logins("edunetwork") if l]
    except Exception as err:  # лёг API или токен без прав агентства
        why.append("список клиентов агентства недоступен (%s), беру DIRECT_CLIENTS_JSON" % err)
    if not logins:
        logins = [c["login"] for c in _json_clients(env, "DIRECT_CLIENTS_JSON", "", why)]
        if not logins and not why:
            why.append("у агентства нет клиентов, DIRECT_CLIENTS_JSON пуст")
    return [DirectAccount(l, token, "RUB", "EDUNETWORK") for l in logins]


def _single(env, token_names, login_names, default_login, label, currency, why):
    token = ""
    for name in token_names:
        token = _get(env, name)
        if token:
            break
    if not token:
        why.append("%s: нет токена в %s" % (label, " / ".join(token_names)))
        return []
    login = ""
    for name in login_names:
        login = _get(env, name)
        if login:
            break
    login = login or default_login
    if not login:
        why.append("%s: не задан Client-Login (%s)" % (label, " / ".join(login_names)))
        return []
    return [DirectAccount(login, token, currency, label)]


def accounts_for(slug: str, env: Mapping[str, str] = os.environ,
                 agency_logins: Optional[Callable[[str], List[str]]] = None
                 ) -> Tuple[List[DirectAccount], List[str]]:
    """Кабинеты проекта и строки «почему пропущен». Пусто + причина, не исключение."""
    if agency_logins is None:
        # Токен из переданного env, а не из os.environ: иначе подставленное
        # окружение тихо ходило бы в сеть с чужим токеном.
        def agency_logins(_slug: str) -> List[str]:
            return agency_logins_by_token(_get(env, "DIRECT_TOKEN"))
    why: List[str] = []
    if slug == "edunetwork":
        found = _edu(env, agency_logins, why)
    elif slug == "lime":
        found = _single(env, ["LIME_DIRECT_TOKEN"], ["LIME_DIRECT_CLIENT_LOGIN"],
                        "", "LIME", "RUB", why)
        found += _single(env, ["LIME_DIRECT_KZ_TOKEN"], ["LIME_DIRECT_KZ_CLIENT_LOGIN"],
                         "", "LIME KZ", "KZT", why)
    elif slug == "bjorn":
        clients = _json_clients(env, "BJORN_DIRECT_CLIENTS_JSON",
                                _get(env, "BJORN_DIRECT_TOKEN"), why)
        found = [DirectAccount(c["login"], c["token"], "RUB", "BJORN")
                 for c in clients if c["token"]]
        if clients and len(found) < len(clients):
            why.append("BJORN: у части клиентов нет токена (BJORN_DIRECT_TOKEN)")
        if not clients and not why:
            why.append("BJORN: нет BJORN_DIRECT_CLIENTS_JSON")
    elif slug == "polinarepik":
        found = _single(env, ["POLINAREPIK_DIRECT_TOKEN", "POLINAREPIK_YANDEX_TOKEN"],
                        ["POLINAREPIK_DIRECT_CLIENT_LOGIN"], "polinarepik-wear",
                        "Polinarepik", "RUB", why)
    elif slug == "meshnflesh":
        found = _single(env, ["MESHNFLESH_DIRECT_TOKEN"], [], "meshnflesh",
                        "mesh-n-flesh", "RUB", why)
    else:
        return [], ["нет кабинетов Директа у проекта %s" % slug]
    return found, why
