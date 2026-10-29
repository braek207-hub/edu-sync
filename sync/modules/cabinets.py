# -*- coding: utf-8 -*-
"""Кабинеты модуля: выбор из настроек (settings.logins) и инвентарь для Panda (module_cabinets).

Выбор: пустой список — все кабинеты проекта из accounts_for, как было до поля logins.
Инвентарь: по каждому кабинету проекта число кампаний и доступ; по нему Panda показывает,
из чего выбирать. Обновляется не чаще INVENTORY_EVERY: такт модулей идёт каждые полчаса,
а состав кабинетов меняется редко.
"""

import re
from datetime import datetime, timedelta
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from sync.modules import direct_api
from sync.modules.accounts import DirectAccount
from sync.placements.direct import DirectError, call

INVENTORY_EVERY = timedelta(hours=1)

# Отказ в доступе к кабинету (ref-v5/errors): 53 — авторизация (токен отозван или чужой),
# 54 — нет прав на клиента, 58 — незаконченная регистрация / приложение без доступа к API,
# 513 — логин не подключён к Директу. Тот же набор, что probe_direct_write_access и
# probe_bidmodifiers считают NO_ACCESS; 152 (недостаточно баллов) там тоже есть, но это
# временный отказ, а не отсутствие доступа — его здесь нет.
ACCESS_CODES = frozenset({53, 54, 58, 513})


def _norm(login: str) -> str:
    # Логин Яндекса регистронезависим: «Ivanov» в настройке — тот же кабинет, что «ivanov».
    return login.strip().lower()


def select_accounts(accts: Sequence[DirectAccount], logins: Iterable
                    ) -> Tuple[List[DirectAccount], List[str]]:
    """Кабинеты, которые модуль обходит, и строки «почему пропущен» для логинов из настройки,
    которых у проекта нет. Пустой список — все кабинеты."""
    wanted = [str(x) for x in (logins or []) if isinstance(x, str) and x.strip()]
    if not wanted:
        return list(accts), []
    keep = {_norm(x) for x in wanted}
    known = {_norm(a.login) for a in accts}
    why = ["%s: нет среди кабинетов проекта — пропущен" % x.strip()
           for x in dict.fromkeys(wanted) if _norm(x) not in known]
    return [a for a in accts if _norm(a.login) in keep], why


def access_denied(err: Exception) -> bool:
    """DirectError с кодом отказа в доступе: текст вида «campaigns [54] Нет прав: …»."""
    m = re.search(r"\[(\d+)\]", str(err))
    return bool(m) and int(m.group(1)) in ACCESS_CODES


def count_campaigns(acc: DirectAccount, call_fn=call) -> int:
    rows = direct_api.get_all(acc.token, acc.login, "campaigns", {}, ["Id"], call_fn=call_fn)
    return len(rows)


def inventory(accts: Sequence[DirectAccount], log: Callable[[str], None],
              call_fn=call) -> List[dict]:
    """Строки module_cabinets. Временный сбой (сеть, 1000, 204) строки не даёт: последнее
    известное состояние кабинета лучше, чем «0 кампаний» по сбою."""
    out: List[dict] = []
    for acc in accts:
        row = {"login": acc.login, "currency": (acc.currency or "RUB").upper()}
        try:
            row.update(campaigns=count_campaigns(acc, call_fn), access="ok")
        except DirectError as err:
            if not access_denied(err):
                log("кабинеты: %s не прочитан (%s)" % (acc.login, err))
                continue
            row.update(campaigns=None, access="denied")
        except Exception as err:  # сеть и прочее: кабинет не должен ронять инвентарь
            log("кабинеты: %s не прочитан (%s: %s)" % (acc.login, type(err).__name__, err))
            continue
        out.append(row)
    return out


def refresh(slug: str, accts: Sequence[DirectAccount], store, now: datetime,
            log: Callable[[str], None], call_fn=call,
            every: timedelta = INVENTORY_EVERY) -> Optional[int]:
    """Обновить инвентарь проекта, если он старше every. Сбой (в том числе нет таблицы —
    миграция Panda не накатана) только пишется в лог: прогон модулей важнее списка.
    Возвращает число записанных строк, None — не обновлялся."""
    if not accts:
        return None
    try:
        last = store.cabinets_checked_at(slug)
        if last is not None and now - last < every:
            return None
        rows = inventory(accts, log, call_fn)
        if rows:
            store.upsert_cabinets(slug, rows)
        return len(rows)
    except Exception as err:
        missing = type(err).__name__ == "UndefinedTable"
        log("кабинеты: инвентарь не записан (%s)" % (
            "нет таблицы module_cabinets — миграция Panda не накатана" if missing
            else "%s: %s" % (type(err).__name__, err)))
        return None
