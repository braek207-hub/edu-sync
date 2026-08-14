# -*- coding: utf-8 -*-
"""
sync/agent/objects.py — снимок структуры кабинета и поисковые запросы.

Версионирование по содержимому: новая строка появляется только когда объект
изменился. Ежедневная копия 55 тысяч объектов дала бы 5 млн строк за квартал
и ничего бы не добавила — структура меняется редко.

Кандидаты в минус-слова считаются здесь же: правило «расход больше трёх CPA при нуле
конверсий» не требует статистики и работает на любом объёме.
"""

import hashlib
import json
from typing import Any, Dict, List

# Поля, по которым объект опознаётся на каждом уровне: (id, кампания, родитель).
_ID_FIELDS = {
    "adgroup": ("Id", "CampaignId", None),
    "keyword": ("Id", "CampaignId", "AdGroupId"),
    "ad": ("Id", "CampaignId", "AdGroupId"),
}


def content_hash(payload: Dict[str, Any]) -> str:
    """Устойчивый хеш содержимого: порядок ключей не влияет, кириллица не экранируется."""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def build_object_rows(
    items: List[Dict[str, Any]], object_level: str, seen_on: str
) -> List[Dict[str, Any]]:
    """Строки снимка структуры для одного уровня."""
    id_field, campaign_field, parent_field = _ID_FIELDS[object_level]
    # Идентификаторы уже лежат отдельными колонками — в payload они только дублируют
    # данные и раздувают JSONB: 198 тыс. объектов заняли 123 МБ (прогон 31788997736).
    dropped = {f for f in (id_field, campaign_field, parent_field) if f}
    out: List[Dict[str, Any]] = []
    for item in items:
        payload = {k: v for k, v in item.items() if k not in dropped}
        out.append({
            "object_level": object_level,
            "object_id": str(item[id_field]),
            "campaign_id": str(item.get(campaign_field, "")),
            "parent_id": str(item[parent_field]) if parent_field and item.get(parent_field) else None,
            "content_hash": content_hash(payload),
            "payload": payload,
            "first_seen": seen_on,
            "last_seen": seen_on,
        })
    return out


def top_queries_by_cost(
    queries: List[Dict[str, Any]], per_campaign: int = 500
) -> List[Dict[str, Any]]:
    """Верх по расходу внутри каждой кампании.

    Кандидаты в минус-слова — это запросы, которые ЖГУТ бюджет; миллион запросов
    с одним кликом не несёт решений, но занял 450 МБ на прогоне 31785888375.
    Отсекаем хвост, сохраняя всё, что реально стоит денег.
    """
    by_campaign: Dict[str, List[Dict[str, Any]]] = {}
    for q in queries:
        by_campaign.setdefault(str(q.get("campaign_id", "")), []).append(q)
    out: List[Dict[str, Any]] = []
    for campaign_id, rows in by_campaign.items():
        rows.sort(key=lambda r: float(r.get("cost") or 0.0), reverse=True)
        out += rows[:per_campaign]
    return out


def minus_word_candidates(
    queries: List[Dict[str, Any]], cpa_limit: float, multiplier: float = 3.0
) -> List[Dict[str, Any]]:
    """Запросы, сжигающие больше multiplier×CPA без единой конверсии."""
    threshold = cpa_limit * multiplier
    return [
        q for q in queries
        if float(q.get("cost") or 0.0) > threshold and int(q.get("conversions") or 0) == 0
    ]
