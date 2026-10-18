# -*- coding: utf-8 -*-
"""Жизненный цикл находок: чистые функции, без базы и без часов (now приходит снаружи)."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

from sync.modules.types import Finding, StoredFinding, fingerprint

REJECT_SUPPRESS_DAYS = 30
EXPIRE_HOURS = 72

# Статусы совпадают с частичным уникальным индексом module_findings.
LIVE_STATUSES = ("open", "approved", "applied", "rollback_requested")

_TRANSITIONS = {
    "open": {"approved", "rejected", "dismissed", "resolved", "expired"},
    "approved": {"applied", "failed"},
    "applied": {"rollback_requested"},
    # failed при откате: Директ отказал в снятии, находка не должна висеть вечным запросом
    "rollback_requested": {"rolled_back", "failed"},
}


def can_transition(src: str, dst: str) -> bool:
    return dst in _TRANSITIONS.get(src, ())


@dataclass
class Reconciled:
    insert: List[Finding] = field(default_factory=list)
    touch: List[Tuple[int, Finding]] = field(default_factory=list)
    resolve: List[int] = field(default_factory=list)
    expire: List[int] = field(default_factory=list)


def reconcile(module_key: str, existing: List[StoredFinding],
              seen: List[Finding], now: datetime) -> Reconciled:
    by_fp: Dict[str, List[StoredFinding]] = {}
    for s in existing:
        by_fp.setdefault(s.fingerprint, []).append(s)

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
            continue
        if any(s.status == "dismissed" for s in rows):
            continue
        # decided_at пуст у отклонённой — трактуем как свежее решение, а не как «давно»
        if any(s.status == "rejected"
               and (s.decided_at is None
                    or now - s.decided_at < timedelta(days=REJECT_SUPPRESS_DAYS))
               for s in rows):
            continue
        out.insert.append(f)

    for s in existing:
        if s.status != "open" or s.fingerprint in seen_fps:
            continue
        if s.action is None:
            out.resolve.append(s.id)
        elif now - s.last_seen_at > timedelta(hours=EXPIRE_HOURS):
            out.expire.append(s.id)
    return out
