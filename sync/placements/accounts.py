# -*- coding: utf-8 -*-
"""Реестр кабинетов Директа для чистильщика площадок.

Кабинет описывается декларативно: где взять токен, какой Client-Login
подставить. Кабинет без доступа не роняет прогон — он пропускается с
внятной строкой, потому что чистильщик ходит по шести клиентам, и потеря
одного не повод оставить остальных грязными.
"""

import json
import os
from typing import Dict, List, NamedTuple, Optional, Tuple


class Account(NamedTuple):
    key: str            # короткое имя кабинета в журнале и логах
    label: str          # человекочитаемое
    token_env: str      # переменная окружения с OAuth-токеном
    login: Optional[str] = None       # Client-Login; None — прямой кабинет
    logins_env: Optional[str] = None  # JSON со списком клиентов агентства
    agency: bool = False              # спросить список клиентов у Директа
    # Площадки, которые в этом кабинете запрещаются всегда: не по вердикту
    # словаря и не по кликам, а решением человека. Список кабинетный —
    # почта и Дзен у одного клиента сливают бюджет, у другого приносят лиды.
    always_block: Tuple[str, ...] = ()

    def token(self) -> str:
        return os.environ.get(self.token_env, "").strip()


# Порядок задаёт очередь обхода. Russever первый: на нём чистильщик
# обкатывался, и его данные — эталон для проверки правил.
ACCOUNTS: List[Account] = [
    Account("russever", "Групп Орсо (Russever)", "RUSSEVER_DIRECT",
            login="orso-groupmedia",
            # Решение Павла 10.09.2026, только для этого кабинета: почтовые
            # интерфейсы и игры Яндекса откручивают бюджет без заявок.
            # Словарь их не режет и резать не должен — mail.ru и yandex.ru
            # это крупные площадки, вопрос ставок, а не мусора; здесь работает
            # не вердикт, а прямое указание.
            always_block=("mail.yandex.ru", "mail.ru", "win.mail.ru",
                          "images.yandex.ru", "mail.rambler.ru",
                          "m.games.yandex.ru")),
    # EDU — агентский кабинет: клиентов два десятка, и список живёт у самого
    # Директа. Секрет DIRECT_CLIENTS_JSON перечисляет только тех, у кого
    # проставлены цели, — чистить надо всех, включая новых, о которых секрет
    # ещё не знает.
    Account("edu", "EDUNETWORK", "DIRECT_TOKEN",
            logins_env="DIRECT_CLIENTS_JSON", agency=True),
    Account("lime", "LIME", "LIME_DIRECT_TOKEN",
            login=os.environ.get("LIME_DIRECT_CLIENT_LOGIN") or None),
    Account("bjorn", "BJORN", "BJORN_DIRECT_TOKEN",
            login=os.environ.get("BJORN_DIRECT_CLIENT_LOGIN") or None),
    Account("polinarepik", "Polinarepik", "POLINA_DIRECT_TOKEN",
            login=os.environ.get("POLINA_DIRECT_CLIENT_LOGIN") or None),
    Account("meshnflesh", "mesh-n-flesh", "MNF_DIRECT_TOKEN",
            login=os.environ.get("MNF_DIRECT_CLIENT_LOGIN") or None),
]

BY_KEY: Dict[str, Account] = {a.key: a for a in ACCOUNTS}


def agency_logins(account: Account) -> List[str]:
    """Клиенты агентства прямо из Директа. Пустой список — не повод падать."""
    from sync.placements.direct import DirectError, call

    out: List[str] = []
    offset = 0
    while True:
        try:
            res = call(account.token(), "", "agencyclients", {
                "method": "get",
                "params": {"SelectionCriteria": {},
                           "FieldNames": ["Login"],
                           "Page": {"Limit": 1000, "Offset": offset}},
            })
        except (DirectError, Exception):
            return out
        chunk = res.get("Clients", [])
        out.extend(str(c.get("Login", "")).strip() for c in chunk
                   if str(c.get("Login", "")).strip())
        if len(chunk) < 1000:
            return out
        offset += len(chunk)


def logins_of(account: Account) -> List[str]:
    """Клиентские логины кабинета.

    У агентского кабинета их несколько (EDU: ВУЗ, СПО, ПроВУЗ…), и чистить
    надо каждый: запрет площадок живёт на кампании, а кампании — у клиента.
    """
    if account.agency:
        logins = agency_logins(account)
        if logins:
            return logins
    if account.logins_env:
        raw = os.environ.get(account.logins_env, "").strip()
        if not raw:
            return []
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            return []
        out = []
        for item in items:
            login = str(item.get("login", "")).strip() if isinstance(item, dict) else str(item).strip()
            if login:
                out.append(login)
        return out
    return [account.login] if account.login else []


def available(keys: Optional[List[str]] = None):
    """Кабинеты, до которых есть доступ, и причины отказа по остальным."""
    ready, skipped = [], []
    for account in ACCOUNTS:
        if keys and account.key not in keys:
            continue
        if not account.token():
            skipped.append((account, "нет токена в %s" % account.token_env))
            continue
        logins = logins_of(account)
        if not logins:
            skipped.append((account, "не задан Client-Login"))
            continue
        ready.append((account, logins))
    return ready, skipped
