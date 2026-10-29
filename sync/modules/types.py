# -*- coding: utf-8 -*-
"""Типы движка модулей Директа: находка, её запись в базе, контекст прогона."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional, Tuple, Union

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
    # время итога раннера (применил/упал/откатил); decided_at пишет только человек
    outcome_at: Optional[datetime] = None


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
    # Цели-лиды проекта (store.project_lead_goals): вторая ступень цепочки leads.lead_goals.
    project_goals: List[int] = field(default_factory=list)
    # Ход прогона {"step", "logins_done", "logins_total"} после каждого логина: пробный
    # прогон показывает его человеку, обычный скан — никому.
    progress: Callable[[dict], None] = lambda _p: None


@dataclass
class RunOutput:
    findings: List[Finding]
    summary: dict = field(default_factory=dict)
    # None — прочитаны все кабинеты ctx; иначе только эти логины (остальные не закрываем)
    scanned_logins: Optional[List[str]] = None
    # (логин, правило) или (логин, правило, кампания), которые прогон не проверил: вход
    # правила не прочитан, правило выключено настройкой или цель не видна кампании (тройка).
    # Их живые находки reconcile не трогает: молчание — не «исправлено».
    unverified: List[Union[Tuple[str, str], Tuple[str, str, int]]] = field(default_factory=list)
    # Строки «почему пропущена» по выбранным кампаниям (settings.campaigns), которых у кабинета
    # нет или которые модулю не подходят: cabinets.unmatched_lines.
    skipped_campaigns: List[str] = field(default_factory=list)
