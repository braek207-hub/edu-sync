# -*- coding: utf-8 -*-
"""Реестр кабинетов Директа для старого входа чистильщика площадок.

Кабинет описывается декларативно: ключ и проект Panda-BI. CLI
(sync.placements_clean) переводит ключи кабинетов в проекты; токены, логины и
пропуск кабинета без доступа — у раннера модулей (sync.modules.accounts).
"""

from typing import Dict, List, NamedTuple


class Account(NamedTuple):
    key: str            # короткое имя кабинета в журнале и логах
    label: str          # человекочитаемое
    slug: str           # проект Panda-BI в реестре sync.modules.accounts
    # always_block и spam_rule кабинета переехали в настройки модуля
    # (project_modules.settings): источник правды теперь они.


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
