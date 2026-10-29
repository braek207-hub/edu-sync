# -*- coding: utf-8 -*-
"""Кабинеты и кампании модуля: выбор из настроек (settings.logins, settings.campaigns) и
инвентарь для Panda (module_cabinets, module_campaigns).

Выбор: пустой logins — все кабинеты проекта из accounts_for, как было до поля. campaigns —
{логин: [Id]}; логина в нём нет — все подходящие модулю кампании кабинета. Выбранные кампании
едут в DirectAccount.campaign_ids, и модуль сужает по ним чтение кампаний (with_ids) и строки
отчётов (keep_rows) — одни и те же функции во всех трёх модулях.
Инвентарь: по каждому кабинету проекта кампании и доступ; по нему Panda показывает, из чего
выбирать. Обновляется не чаще INVENTORY_EVERY: такт модулей идёт каждые полчаса, а состав
кабинетов и кампаний меняется редко.
"""

import dataclasses
import re
from datetime import datetime, timedelta
from typing import (Any, Callable, Collection, Dict, Iterable, List, Mapping, Optional,
                    Sequence, Tuple)

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

# Архивные не читаем и не пишем: выбрать их для модуля незачем, а Директ держит их тысячами.
LIVE_STATES = ["ON", "OFF", "SUSPENDED", "ENDED", "CONVERTED"]
# Блоки стратегии по типам кампаний — те же имена, что уже ходят в прод (agent/segments.py,
# lime_direct.py, probe_bjorn_*): в стратегии видно, включены ли поиск и сеть.
STRATEGY_BLOCKS = {
    "TEXT_CAMPAIGN": "TextCampaign",
    "UNIFIED_CAMPAIGN": "UnifiedCampaign",
    "MOBILE_APP_CAMPAIGN": "MobileAppCampaign",
    "CPM_BANNER_CAMPAIGN": "CpmBannerCampaign",
    "SMART_CAMPAIGN": "SmartCampaign",
}
SERVING_OFF = "SERVING_OFF"


def _norm(login: str) -> str:
    # Логин Яндекса регистронезависим: «Ivanov» в настройке — тот же кабинет, что «ivanov».
    return login.strip().lower()


def select_accounts(accts: Sequence[DirectAccount], logins: Iterable,
                    campaigns: Optional[Mapping[str, Sequence[int]]] = None
                    ) -> Tuple[List[DirectAccount], List[str]]:
    """Кабинеты, которые модуль обходит (с выбранными кампаниями в campaign_ids), и строки
    «почему пропущен» для логинов настройки, которых у проекта нет. Пустой logins — все."""
    wanted = [str(x) for x in (logins or []) if isinstance(x, str) and x.strip()]
    known = {_norm(a.login) for a in accts}
    why: List[str] = []
    picked = list(accts)
    if wanted:
        keep = {_norm(x) for x in wanted}
        why += ["%s: нет среди кабинетов проекта — пропущен" % x.strip()
                for x in dict.fromkeys(wanted) if _norm(x) not in known]
        picked = [a for a in accts if _norm(a.login) in keep]
    chosen = {_norm(k): v for k, v in (campaigns or {}).items() if v}
    walked = {_norm(a.login) for a in picked}
    for login, ids in (campaigns or {}).items():
        if ids and _norm(login) not in walked:
            why.append("%s: кампании выбраны, но кабинет %s — пропущены" % (
                login, "не из проекта" if _norm(login) not in known else "не выбран"))
    out = [dataclasses.replace(a, campaign_ids=tuple(sorted({int(i) for i in
                                                             chosen[_norm(a.login)]})))
           if _norm(a.login) in chosen else a for a in picked]
    return out, why


def scanned_campaigns(accts: Sequence[DirectAccount]) -> Dict[str, Collection[int]]:
    """{логин: выбранные кампании} для reconcile: только кабинеты с выбором."""
    return {a.login: frozenset(a.campaign_ids) for a in accts if a.campaign_ids is not None}


def with_ids(acc: DirectAccount, criteria: Optional[dict] = None) -> dict:
    """SelectionCriteria чтения кампаний, суженная до выбранных: невыбранные Директ не отдаёт."""
    crit = dict(criteria or {})
    if acc.campaign_ids is not None:
        crit["Ids"] = list(acc.campaign_ids)
    return crit


def keep_rows(acc: DirectAccount, rows: List[dict], key: str = "campaign_id") -> List[dict]:
    """Строки отчёта только выбранных кампаний. Отчёт площадок кабинетный: заказать его по
    кампаниям общий клиент старого чистильщика не умеет, поэтому сужаем строки."""
    if acc.campaign_ids is None:
        return rows
    ids = set(acc.campaign_ids)
    out = []
    for r in rows:
        try:
            if int(r.get(key) or 0) in ids:
                out.append(r)
        except (TypeError, ValueError):
            continue
    return out


def unmatched_lines(acc: DirectAccount, suitable: Iterable[Any]) -> List[str]:
    """«Почему пропущена» по выбранным кампаниям, которых нет среди подходящих модулю: нет в
    кабинете, архивная, остановлена или не того типа."""
    if acc.campaign_ids is None:
        return []
    ok = set()
    for x in suitable:
        try:
            ok.add(int(x))
        except (TypeError, ValueError):
            continue
    return ["%s: кампания %d не найдена или не подходит модулю — пропущена" % (acc.login, cid)
            for cid in acc.campaign_ids if cid not in ok]


def access_denied(err: Exception) -> bool:
    """DirectError с кодом отказа в доступе: текст вида «campaigns [54] Нет прав: …»."""
    m = re.search(r"\[(\d+)\]", str(err))
    return bool(m) and int(m.group(1)) in ACCESS_CODES


def _channel_on(strategy: dict, channel: str) -> Optional[bool]:
    # Тот же признак, что agent/headroom.placement_mode: канал включён, если тип его
    # стратегии назван и не SERVING_OFF. Нет блока — неизвестно, а не «выключен».
    kind = (strategy.get(channel) or {}).get("BiddingStrategyType")
    if not kind:
        return None
    return kind != SERVING_OFF


def campaign_row(c: dict) -> dict:
    block = STRATEGY_BLOCKS.get(c.get("Type") or "")
    strategy = ((c.get(block) or {}).get("BiddingStrategy") or {}) if block else {}
    return {"id": int(c["Id"]), "name": str(c.get("Name") or ""), "type": c.get("Type") or "",
            "state": c.get("State") or "", "search": _channel_on(strategy, "Search"),
            "network": _channel_on(strategy, "Network")}


def read_campaigns(acc: DirectAccount, call_fn=call) -> List[dict]:
    """Неархивные кампании кабинета со стратегией. Блок стратегии, которого Директ не принял
    (4000/8000), не роняет инвентарь: кампании остаются, поиск и сеть — неизвестны."""
    blocks = {v + "FieldNames": ["BiddingStrategy"] for v in STRATEGY_BLOCKS.values()}
    fields = ["Id", "Name", "Type", "State"]
    crit = {"States": LIVE_STATES}
    try:
        rows = direct_api.get_all(acc.token, acc.login, "campaigns", crit, fields,
                                  extra=blocks, call_fn=call_fn)
    except DirectError as err:
        if not direct_api.is_unknown_field_error(err):
            raise
        rows = direct_api.get_all(acc.token, acc.login, "campaigns", crit, fields,
                                  call_fn=call_fn)
    return [campaign_row(c) for c in rows if c.get("State") != "ARCHIVED"]


def inventory(accts: Sequence[DirectAccount], log: Callable[[str], None],
              call_fn=call) -> Tuple[List[dict], Dict[str, List[dict]]]:
    """Строки module_cabinets и кампании прочитанных кабинетов {логин: [кампания]}. Временный
    сбой (сеть, 1000, 204) строки не даёт: последнее известное состояние лучше, чем «0
    кампаний» по сбою. У кабинета без доступа кампании не читались — их список не трогаем."""
    out: List[dict] = []
    camps: Dict[str, List[dict]] = {}
    seen = set()
    for acc in accts:
        # дубль логина в конфиге проекта: второй upsert той же строки в одном запросе падает
        if acc.login in seen:
            continue
        seen.add(acc.login)
        row = {"login": acc.login, "currency": (acc.currency or "RUB").upper()}
        try:
            found = read_campaigns(acc, call_fn)
        except DirectError as err:
            if not access_denied(err):
                log("кабинеты: %s не прочитан (%s)" % (acc.login, err))
                continue
            row.update(campaigns=None, access="denied")
        except Exception as err:  # сеть и прочее: кабинет не должен ронять инвентарь
            log("кабинеты: %s не прочитан (%s: %s)" % (acc.login, type(err).__name__, err))
            continue
        else:
            row.update(campaigns=len(found), access="ok")
            camps[acc.login] = found
        out.append(row)
    return out, camps


def refresh(slug: str, accts: Sequence[DirectAccount], store, now: datetime,
            log: Callable[[str], None], call_fn=call,
            every: timedelta = INVENTORY_EVERY) -> Optional[int]:
    """Обновить инвентарь проекта, если он старше every. Сбой (в том числе нет таблицы —
    миграция Panda не накатана) только пишется в лог: прогон модулей важнее списка.
    Возвращает число записанных кабинетов, None — не обновлялся."""
    if not accts:
        return None
    try:
        last = store.cabinets_checked_at(slug)
        if last is not None and now - last < every:
            return None
        rows, camps = inventory(accts, log, call_fn)
        if rows:
            store.upsert_cabinets(slug, rows)
        if camps:
            store.replace_campaigns(slug, camps)
        return len(rows)
    except Exception as err:
        missing = type(err).__name__ == "UndefinedTable"
        log("кабинеты: инвентарь не записан (%s)" % (
            "нет таблицы module_cabinets или module_campaigns — миграция Panda не накатана"
            if missing else "%s: %s" % (type(err).__name__, err)))
        return None
