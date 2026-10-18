# -*- coding: utf-8 -*-
"""Жизненный цикл находок: чистые функции, без базы и без часов (now приходит снаружи)."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Collection, Dict, List, Optional, Tuple

from sync.modules.types import Finding, StoredFinding, fingerprint

REJECT_SUPPRESS_DAYS = 30
EXPIRE_HOURS = 72
# Упавшее применение не повторяем каждые 30 минут: ошибка Директа за это время не исчезнет.
FAILED_RETRY_HOURS = 24

# Статусы совпадают с частичным уникальным индексом module_findings.
LIVE_STATUSES = ("open", "approved", "applied", "rollback_requested")

_TRANSITIONS = {
    "open": {"approved", "rejected", "dismissed", "resolved", "expired"},
    "approved": {"applied", "failed"},
    "applied": {"rollback_requested"},
    # applied при откате: Директ отказал в снятии, запрет остался в силе
    "rollback_requested": {"rolled_back", "applied"},
}


def can_transition(src: str, dst: str) -> bool:
    return dst in _TRANSITIONS.get(src, ())


@dataclass
class Reconciled:
    insert: List[Finding] = field(default_factory=list)
    touch: List[Tuple[int, Finding]] = field(default_factory=list)
    resolve: List[int] = field(default_factory=list)
    expire: List[int] = field(default_factory=list)
    # одобренные/применённые, увиденные снова: трогаем только last_seen_at и run_id
    seen_live: List[int] = field(default_factory=list)


def _suppressed(s: StoredFinding, now: datetime) -> bool:
    """Не вставлять заново: решение человека или недавний отказ Директа ещё в силе."""
    # даты нет — берём следующую по списку: «давно» нельзя выводить из отсутствия даты
    if s.status == "rejected":
        return now - (s.decided_at or s.last_seen_at) < timedelta(days=REJECT_SUPPRESS_DAYS)
    # итоги раннера меряем от момента итога, а не решения человека (одобрить могли давно)
    ref = s.outcome_at or s.decided_at or s.last_seen_at
    if s.status == "rolled_back":
        # человек снял запрет, auto-режим не должен вернуть его сразу
        return now - ref < timedelta(days=REJECT_SUPPRESS_DAYS)
    if s.status == "failed":
        return now - ref < timedelta(hours=FAILED_RETRY_HOURS)
    return False


def _action_key(f: Finding):
    """Тот же шаг над тем же объектом, под любым правилом; у находок без action ключа нет."""
    if f.action is None:
        return None
    return (f.login, f.campaign_id, f.object_id, f.action.get("op"))


def reconcile(module_key: str, existing: List[StoredFinding],
              seen: List[Finding], now: datetime,
              scanned_logins: Optional[Collection[str]] = None) -> Reconciled:
    """scanned_logins: кабинеты, прочитанные в этом прогоне (None — все). Открытые находки
    непрочитанных кабинетов не закрываются; находки с login "" относятся к проекту целиком
    и считаются прочитанными, когда scanned_logins непуст.
    """
    by_fp: Dict[str, List[StoredFinding]] = {}
    for s in existing:
        by_fp.setdefault(s.fingerprint, []).append(s)

    # отказ человека относится к действию, а не к имени правила: stop_word после отката
    # word_zero_conv имеет другой fingerprint, но то же действие
    by_action: Dict[tuple, List[StoredFinding]] = {}
    for s in existing:
        key = _action_key(s)
        if key is not None:
            by_action.setdefault(key, []).append(s)

    out = Reconciled()
    seen_fps = set()
    for f in seen:
        fp = fingerprint(module_key, f)
        if fp in seen_fps:
            continue
        seen_fps.add(fp)
        rows = by_fp.get(fp, [])
        live = [s for s in rows if s.status in LIVE_STATUSES]
        if live:
            # менять детали у approved/applied нельзя: человек одобрил именно то, что видел
            opened = [s for s in live if s.status == "open"]
            if opened:
                out.touch.append((opened[0].id, f))
            else:
                out.seen_live.append(live[0].id)
            continue
        akey = _action_key(f)
        blockers = rows + (by_action.get(akey, []) if akey is not None else [])
        if any(s.status == "dismissed" for s in blockers):
            continue
        if any(_suppressed(s, now) for s in blockers):
            continue
        out.insert.append(f)

    for s in existing:
        if s.status != "open" or s.fingerprint in seen_fps:
            continue
        # кабинет, который не удалось прочитать, ничего не «не показал»
        # login "" — находка уровня проекта: прочитана, если прочитан хоть один кабинет
        if (scanned_logins is not None and s.login not in scanned_logins
                and not (s.login == "" and scanned_logins)):
            continue
        if s.action is None:
            out.resolve.append(s.id)
        elif now - s.last_seen_at > timedelta(hours=EXPIRE_HOURS):
            out.expire.append(s.id)
    return out
