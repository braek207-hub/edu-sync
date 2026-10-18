# -*- coding: utf-8 -*-
"""Реестр кабинетов Директа для чистильщика площадок.

Кабинет описывается декларативно: ключ, проект и кабинетные правила; токены и
логины даёт sync.modules.accounts. Кабинет без доступа не роняет прогон — он
пропускается с внятной строкой, потому что чистильщик ходит по пяти клиентам,
и потеря одного не повод оставить остальных грязными.
"""

import os
from typing import Dict, List, NamedTuple, Optional

from sync.modules.accounts import accounts_for


class Account(NamedTuple):
    key: str            # короткое имя кабинета в журнале и логах
    label: str          # человекочитаемое
    slug: str           # проект Panda-BI в реестре sync.modules.accounts
    # always_block и spam_rule кабинета переехали в настройки модуля
    # (project_modules.settings): источник правды теперь они.
    # Токен подставляет available(): имена переменных и их разбор живут в
    # sync.modules.accounts, чтобы не разъезжаться с остальными модулями.
    token_value: str = ""

    def token(self) -> str:
        return self.token_value


# Russever (Групп Орсо, orso-groupmedia) снят 06.10.2026: работа с клиентом
# закончена, в чужой кабинет чистильщик больше не ходит. Вместе с ним выключены
# три такта Actions — russever-checkup, russever-close-city, russever-creo.
# Если клиент вернётся, запись восстанавливается из истории файла: кабинетный
# always_block был («mail.yandex.ru», «mail.ru», «win.mail.ru»,
# «images.yandex.ru», «mail.rambler.ru», «m.games.yandex.ru»).
ACCOUNTS: List[Account] = [
    # EDU — агентский кабинет: клиентов два десятка, и список живёт у самого
    # Директа. Секрет DIRECT_CLIENTS_JSON перечисляет только тех, у кого
    # проставлены цели, — чистить надо всех, включая новых, о которых секрет
    # ещё не знает.
    # Решение Павла 14.09.2026: площадки с аномально частым «Спасибо» выключать
    # (спам-заявки, не студенты) — теперь spam_rule=true в настройках EDU.
    Account("edu", "EDUNETWORK", "edunetwork"),
    Account("lime", "LIME", "lime"),
    Account("bjorn", "BJORN", "bjorn"),
    Account("polinarepik", "Polinarepik", "polinarepik"),
    Account("meshnflesh", "mesh-n-flesh", "meshnflesh"),
]

BY_KEY: Dict[str, Account] = {a.key: a for a in ACCOUNTS}


def available(keys: Optional[List[str]] = None, env=os.environ):
    """Кабинеты, до которых есть доступ, и причины отказа по остальным."""
    ready, skipped = [], []
    for account in ACCOUNTS:
        if keys and account.key not in keys:
            continue
        found, why = accounts_for(account.slug, env)
        if not found:
            skipped.append((account, "; ".join(why) or "нет кабинетов Директа"))
            continue
        if why:
            # Частичный пропуск (нет KZ-токена, запасной список клиентов) —
            # в журнале прогона, иначе кабинет теряется молча.
            skipped.append((account, "; ".join(why)))
        # Токены у клиентов одного проекта бывают разные (BJORN): прогон идёт
        # по группам с общим токеном, ключ журнала остаётся проектным.
        by_token: Dict[str, List[str]] = {}
        for d in found:
            by_token.setdefault(d.token, []).append(d.login)
        for token, logins in by_token.items():
            ready.append((account._replace(token_value=token), logins))
    return ready, skipped
