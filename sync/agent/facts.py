# -*- coding: utf-8 -*-
"""
sync/agent/facts.py — ежедневный снимок фактов автопилота.

Грань витрины: date × campaign_id. Это не косметика — недельные срезы на клиенте
фильтруют строки по дате, и «репрезентативная строка кампании» теряется при
любом срезе короче месяца (грабли ML-дашборда Ф1c).

Σ p_pay считается ТОЛЬКО по точке скоринга at_creation: post_connection — подвыборка
дозвонившихся, её сложение с at_creation задваивает лиды.

Глубина CRM хранится суммами и счётчиками, а не средними: среднее по среднему не
складывается при перегруппировке (по неделям, по направлениям). Время до дозвона
нужно детектору насыщения продаж — рост при растущем объёме означает «упёрлись
в отдел продаж», а не «трафик испортился».
"""

from datetime import date, datetime
from typing import Any, Dict, List

SCORING_POINT = "at_creation"


def assemble_facts(
    direct_rows: List[Dict[str, Any]],
    lead_rows: List[Dict[str, Any]],
    score_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Собирает витрину фактов из трёх источников. Чистая функция."""
    p_pay_by_lead = {
        r["lead_id"]: float(r.get("p_pay") or 0.0)
        for r in score_rows
        if r.get("scoring_point") == SCORING_POINT
    }

    facts: Dict[tuple, Dict[str, Any]] = {}

    def _slot(fact_date: str, campaign_id: str) -> Dict[str, Any]:
        key = (fact_date, campaign_id)
        if key not in facts:
            facts[key] = {
                "fact_date": fact_date,
                "campaign_id": campaign_id,
                "campaign_name": None,
                "project": None,
                "direction": None,
                "cost": 0.0,
                "clicks": 0,
                "impressions": 0,
                "leads": 0,
                "eff_leads": 0,
                "sum_p_pay": 0.0,
                "payments_fact": 0,
                "avg_impr_pos": 0.0,
                "auction_win_share": 0.0,
                "avg_traffic_vol": 0.0,
                # Накопители взвешенных сумм Директа: w_* = значение × показы.
                # Средним они становятся в самом конце, делением на показы дня.
                # Держать их отдельно обязательно: строк источника на пару
                # «день × кампания» бывает больше одной, и присваивание среднего
                # по ходу цикла теряло бы все, кроме последней.
                "_w_impr_pos": 0.0,
                "_w_win_share": 0.0,
                "_w_traffic_vol": 0.0,
                "connected_leads": 0,
                "deals": 0,
                "mins_to_connection_sum": 0.0,
                "mins_to_connection_count": 0,
                "days_to_pay_sum": 0.0,
                "days_to_pay_count": 0,
                "revenue": 0.0,
                "conversions": 0,
            }
        return facts[key]

    for r in direct_rows:
        slot = _slot(str(r["date"]), str(r["campaign_id"]))
        slot["campaign_name"] = r.get("campaign_name")
        slot["project"] = r.get("project")
        slot["direction"] = r.get("direction")
        slot["cost"] += float(r.get("cost") or 0.0)
        slot["clicks"] += int(r.get("clicks") or 0)
        # Конверсии ЦЕЛЕЙ Директа — валюта целевого CPA (Э3.5), отдельная от
        # лидов CRM: цель в кабинете назначается за конверсию цели, а не за
        # эффективный лид, и коэффициент между ними у кампаний разный.
        slot["conversions"] += int(r.get("conversions") or 0)
        slot["impressions"] += int(r.get("impressions") or 0)
        slot["_w_impr_pos"] += float(r.get("w_avg_impr_pos") or 0.0)
        slot["_w_win_share"] += float(r.get("w_auction_win_share") or 0.0)
        slot["_w_traffic_vol"] += float(r.get("w_avg_traffic_vol") or 0.0)

    for r in lead_rows:
        slot = _slot(str(r["created_date"]), str(r["campaign_id"]))
        slot["leads"] += 1
        if r.get("is_eff"):
            slot["eff_leads"] += 1
        if r.get("is_paid"):
            slot["payments_fact"] += 1
        slot["sum_p_pay"] += p_pay_by_lead.get(r["lead_id"], 0.0)

        if r.get("is_connected"):
            slot["connected_leads"] += 1
        if r.get("is_deal"):
            slot["deals"] += 1
        slot["revenue"] += float(r.get("revenue") or 0.0)

        created_ts, connected_ts = r.get("created_ts"), r.get("connected_ts")
        if created_ts and connected_ts:
            delta = datetime.fromisoformat(str(connected_ts)) - datetime.fromisoformat(str(created_ts))
            slot["mins_to_connection_sum"] += delta.total_seconds() / 60.0
            slot["mins_to_connection_count"] += 1

        payment_date = r.get("payment_date")
        if payment_date:
            days = (date.fromisoformat(str(payment_date)) - date.fromisoformat(str(r["created_date"]))).days
            slot["days_to_pay_sum"] += float(days)
            slot["days_to_pay_count"] += 1

    for slot in facts.values():
        impressions = slot["impressions"]
        for target, source in (("avg_impr_pos", "_w_impr_pos"),
                               ("auction_win_share", "_w_win_share"),
                               ("avg_traffic_vol", "_w_traffic_vol")):
            weighted = slot.pop(source)
            slot[target] = round(weighted / impressions, 4) if impressions else 0.0

    return sorted(facts.values(), key=lambda x: (x["fact_date"], x["campaign_id"]))
