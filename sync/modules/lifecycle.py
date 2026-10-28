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
              scanned_logins: Optional[Collection[str]] = None,
              unverified: Collection[tuple] = ()) -> Reconciled:
    """scanned_logins: кабинеты, прочитанные в этом прогоне (None — все). Открытые находки
    непрочитанных кабинетов не закрываются; находки с login "" относятся к проекту целиком
    и считаются прочитанными, когда scanned_logins непуст.

    unverified: пары (логин, правило) и тройки (логин, правило, кампания), которые прогон
    не проверил (RunOutput.unverified). Их открытые находки не закрываются и не истекают:
    статус и first_seen_at остаются. Тройка держит только находки своей кампании.
    """
    skip_pairs = {tuple(u) for u in unverified if len(u) == 2}
    skip_triples = {(u[0], u[1], int(u[2])) for u in unverified if len(u) == 3}
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
    inserted_actions = set()
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
        if akey is not None:
            if akey in inserted_actions:
                continue
            # то же действие уже живёт под другим правилом: второй записи в очереди не нужно,
            # а в auto обе применились бы к одному элементу Директа
            live_a = [s for s in by_action.get(akey, []) if s.status in LIVE_STATUSES]
            if live_a:
                a = next((s for s in live_a if s.status == "open"), live_a[0])
                seen_fps.add(a.fingerprint)
                if a.status == "open":
                    out.touch.append((a.id, f))
                else:
                    out.seen_live.append(a.id)
                continue
        blockers = rows + (by_action.get(akey, []) if akey is not None else [])
        if any(s.status == "dismissed" for s in blockers):
            continue
        if any(_suppressed(s, now) for s in blockers):
            continue
        out.insert.append(f)
        if akey is not None:
            inserted_actions.add(akey)

    # одну и ту же находку могли подтвердить и по своему fingerprint, и по действию
    out.touch = list({fid: (fid, f) for fid, f in reversed(out.touch)}.values())[::-1]
    out.seen_live = list(dict.fromkeys(out.seen_live))

    for s in existing:
        if s.status != "open" or s.fingerprint in seen_fps:
            continue
        # кабинет, который не удалось прочитать, ничего не «не показал»
        # login "" — находка уровня проекта: прочитана, если прочитан хоть один кабинет
        if (scanned_logins is not None and s.login not in scanned_logins
                and not (s.login == "" and scanned_logins)):
            continue
        # правило в этом кабинете не отработало: «не нашли» здесь не значит «устранено»
        if (s.login, s.rule) in skip_pairs:
            continue
        # цель не видна одной кампании: молчание правила там не значит «устранено»
        if s.campaign_id is not None and (s.login, s.rule, int(s.campaign_id)) in skip_triples:
            continue
        if s.action is None:
            out.resolve.append(s.id)
        elif now - s.last_seen_at > timedelta(hours=EXPIRE_HOURS):
            out.expire.append(s.id)
    return out
