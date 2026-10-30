# -*- coding: utf-8 -*-
"""Реестр кабинетов Директа для старого входа чистильщика площадок.

Кабинет проекта Panda-BI описывается ключом и проектом: CLI
(sync.placements_clean) переводит ключи в проекты, а токены, логины и пропуск
кабинета без доступа — у раннера модулей (sync.modules.accounts).

Кабинет вне проектов Panda-BI (slug=None) модули вести не могут — у них нет
строки в dashboards. Такой кабинет чистит прежний движок старого входа, и для
него здесь остаются токен, логин и список своих кампаний. С 30.10.2026 таких
кабинетов нет (последний, ОШ Синергия, стал проектом synergy): механизм
оставлен на случай нового кабинета без проекта.
"""

import os
from typing import Dict, List, NamedTuple, Optional, Tuple


class Account(NamedTuple):
    key: str            # короткое имя кабинета в журнале и логах
    label: str          # человекочитаемое
    # Проект Panda-BI в реестре sync.modules.accounts; None — кабинет вне проектов.
    # always_block и spam_rule кабинетов проектов переехали в настройки модуля
    # (project_modules.settings): источник правды теперь они.
    slug: Optional[str]
    # Дальше — только для кабинета вне проектов.
    token_env: str = ""               # переменная окружения с OAuth-токеном
    login: Optional[str] = None       # Client-Login
    # Чистить только эти кампании. Нужно там, где кабинет общий: в чужие
    # кампании клиента чистильщик не ходит, даже если они запущены.
    campaign_ids: Tuple[int, ...] = ()

    def token(self) -> str:
        return os.environ.get(self.token_env, "").strip() if self.token_env else ""


# Russever (Групп Орсо, orso-groupmedia) снят 06.10.2026: работа с клиентом
# закончена, в чужой кабинет чистильщик больше не ходит. Вместе с ним выключены
# три такта Actions — russever-checkup, russever-close-city, russever-creo.
# Если клиент вернётся, запись восстанавливается из истории файла: кабинетный
# always_block был («mail.yandex.ru», «mail.ru», «win.mail.ru»,
# «images.yandex.ru», «mail.rambler.ru», «m.games.yandex.ru»).
ACCOUNTS: List[Account] = [
    # EDU — агентский кабинет: клиентов два десятка, и список живёт у самого
    # Директа. Решение Павла 14.09.2026: площадки с аномально частым «Спасибо»
    # выключать (спам-заявки, не студенты) — теперь spam_rule=true в настройках EDU.
    Account("edu", "EDUNETWORK", "edunetwork"),
    Account("lime", "LIME", "lime"),
    # BJORN с 26.10.2026: логины и токены — из BJORN_DIRECT_CLIENTS_JSON
    # (sync.modules.accounts, как у sync/bjorn и настроек кампаний).
    Account("bjorn", "BJORN", "bjorn"),
    Account("polinarepik", "Polinarepik", "polinarepik"),
    Account("meshnflesh", "mesh-n-flesh", "meshnflesh"),
    # ОШ Синергия с 30.10.2026 — проект synergy: кабинет, токен и ограждение двух
    # своих кампаний общего кабинета — в sync.modules.accounts (SYNERGY_SCHOOL_*). До того
    # её чистил старый движок; ключ оставлен, чтобы прежний вызов CLI
    # (--accounts synergy_school) шёл в раннер модулей, а не во второй писатель.
    Account("synergy_school", "ОШ Синергия (vpo-goroda)", "synergy"),
]

BY_KEY: Dict[str, Account] = {a.key: a for a in ACCOUNTS}


def legacy_accounts(keys: Optional[List[str]] = None) -> List[Account]:
    """Кабинеты вне проектов Panda-BI (их чистит старый движок)."""
    return [a for a in ACCOUNTS if a.slug is None and (not keys or a.key in keys)]
