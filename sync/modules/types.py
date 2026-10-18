# -*- coding: utf-8 -*-
"""Типы движка модулей Директа: находка, её запись в базе, контекст прогона."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional

from sync.modules.accounts import DirectAccount


@dataclass
class Finding:
    rule: str
    severity: str
    title: str
    login: str
    campaign_id: Optional[int]
    campaign_name: str
    object_type: str
    object_id: str
    details: dict
    # None — находка только для отчёта, применять нечего
    action: Optional[dict] = None


def fingerprint(module_key: str, f: Finding) -> str:
    return f"{module_key}:{f.rule}:{f.login}:{f.campaign_id or ''}:{f.object_id}"


# kw_only: у родителя есть поле с умолчанием, а у записи из базы обязательные поля после него
@dataclass(kw_only=True)
class StoredFinding(Finding):
    id: int
    status: str
    fingerprint: str
    last_seen_at: datetime
    decided_at: Optional[datetime] = None


@dataclass
class ApplyResult:
    ok: bool
    response: dict
    error: Optional[str] = None


@dataclass
class Context:
    slug: str
    module_key: str
    mode: str
    settings: dict
    accounts: List[DirectAccount]
    now: datetime
    log: Callable[[str], None]


@dataclass
class RunOutput:
    findings: List[Finding]
    summary: dict = field(default_factory=dict)
    # None — прочитаны все кабинеты ctx; иначе только эти логины (остальные не закрываем)
    scanned_logins: Optional[List[str]] = None
