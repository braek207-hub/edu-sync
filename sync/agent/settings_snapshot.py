# -*- coding: utf-8 -*-
"""
sync/agent/settings_snapshot.py — версионированный снимок настроек кампаний.

Источник — витрина edu_campaign_settings, которую наполняет sync/edu_direct_settings.py.
Задача снимка: зафиксировать, КОГДА настройка изменилась. Без этого квазиэксперименты
могут найти скачок метрики, но не могут сказать, какое изменение его вызвало.

Волатильные поля (расход за сегодня, время последнего изменения, статус модерации)
исключаются из хеша: иначе новая версия писалась бы каждый день.
"""

from typing import Any, Dict, List

from sync.agent.objects import content_hash

# Поля, которые меняются сами по себе и настройкой не являются.
VOLATILE_FIELDS = {
    "lastChange", "todaySpend", "statusClarification", "status",
    "sum", "sumSpent", "sumAvailableForTransfer", "funds",
}


def normalize_settings(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Отбрасывает волатильные поля перед хешированием."""
    return {k: v for k, v in raw.items() if k not in VOLATILE_FIELDS}


def build_snapshot_rows(
    settings_by_campaign: Dict[str, Dict[str, Any]], seen_on: str
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for campaign_id, raw in sorted(settings_by_campaign.items()):
        settings = normalize_settings(raw or {})
        out.append({
            "campaign_id": str(campaign_id),
            "content_hash": content_hash(settings),
            "settings": settings,
            "first_seen": seen_on,
            "last_seen": seen_on,
        })
    return out
