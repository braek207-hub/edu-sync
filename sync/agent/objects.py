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
from typing import Any, Dict, List, Optional

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


# Правило трёх: при N испытаниях и НУЛЕ успехов истинная вероятность с 95 %
# уверенностью не выше 3/N. Отсюда и порог наблюдаемости: пока 3/N выше
# базовой конверсии кабинета, «ноль конверсий» означает лишь, что фразу мало
# показывали. Приговор по такому нулю — самый частый способ выкосить живой
# трафик, и именно он делает ручную минусацию опасной.
ZERO_CONVERSION_RULE_OF_THREE = 3.0

# Во сколько раз фактическая цена конверсии фразы должна превышать допустимую,
# чтобы фраза считалась кандидатом. Не 1.0: у отдельной фразы оценка шумная,
# и резать по краю значит резать шум.
CPA_OVERSHOOT = 2.0


def minus_word_candidates(
    queries: List[Dict[str, Any]], cpa_limit: float,
    multiplier: float = CPA_OVERSHOOT,
    base_conversion: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Фразы, которые не окупаются, — с причиной по каждой.

    Оценивается ФРАЗА ЦЕЛИКОМ (агрегат по всем её строкам «фраза × кампания ×
    окно»), а не отдельная строка: правило, применённое к строке, требовало от
    одного окна одной кампании расхода в несколько CPA — такого почти не
    бывает, и на бою кандидатов выходило НОЛЬ при 678 фразах без единой
    конверсии на 5,4 млн ₽ за шесть недель.

    Две причины попасть в кандидаты, обе экономические:

      * zero_conversions — конверсий нет, кликов ХВАТАЕТ, чтобы это что-то
        значило (см. правило трёх), и расход фразы уже превысил цену, которую
        мы согласны платить за конверсию по верхней границе.
      * cpa_above_limit — конверсии есть, но цена в multiplier раз выше
        допустимой: фраза с плохой, но ненулевой конверсией жжёт не меньше.

    base_conversion — конверсия кабинета (доля). Не передана — считается по
    самому набору: сколько конверсий на клик дают все фразы вместе.
    """
    if cpa_limit <= 0:
        return []

    totals: Dict[str, Dict[str, Any]] = {}
    clicks_total = 0
    conversions_total = 0
    for q in queries:
        phrase = str(q.get("query") or "")
        if not phrase:
            continue
        slot = totals.setdefault(phrase, {
            "query": phrase, "cost": 0.0, "clicks": 0, "conversions": 0,
            "campaigns": set(),
        })
        clicks = int(q.get("clicks") or 0)
        conversions = int(q.get("conversions") or 0)
        slot["cost"] += float(q.get("cost") or 0.0)
        slot["clicks"] += clicks
        slot["conversions"] += conversions
        slot["campaigns"].add(str(q.get("campaign_id") or ""))
        clicks_total += clicks
        conversions_total += conversions

    base = (float(base_conversion) if base_conversion is not None
            else (conversions_total / clicks_total if clicks_total else 0.0))
    # Кликов, при которых отсутствие конверсий уже значимо: столько, чтобы
    # верхняя граница правила трёх опустилась ниже базовой конверсии.
    min_clicks_to_judge = (ZERO_CONVERSION_RULE_OF_THREE / base
                           if base > 0 else float("inf"))

    out: List[Dict[str, Any]] = []
    for slot in totals.values():
        cost, clicks, conversions = slot["cost"], slot["clicks"], slot["conversions"]
        if cost <= 0 or clicks <= 0:
            continue
        if conversions > 0:
            cpa = cost / conversions
            if cpa <= cpa_limit * multiplier:
                continue
            reason = "cpa_above_limit"
        else:
            if clicks < min_clicks_to_judge:
                continue
            # Даже если истинная конверсия равна верхней границе правила трёх,
            # фраза принесла бы не больше трёх конверсий — и всё равно дороже
            # допустимого.
            cpa = cost / ZERO_CONVERSION_RULE_OF_THREE
            if cpa <= cpa_limit:
                continue
            reason = "zero_conversions"
        out.append({
            "query": slot["query"],
            "cost": round(cost, 2),
            "clicks": clicks,
            "conversions": conversions,
            "cpa": round(cpa, 2),
            "reason": reason,
            "campaigns": sorted(c for c in slot["campaigns"] if c),
        })
    out.sort(key=lambda r: -r["cost"])
    return out
