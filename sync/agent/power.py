# -*- coding: utf-8 -*-
"""
sync/agent/power.py — отчёт статистической мощности кабинета.

Отвечает на открытый вопрос спеки: сколько объектов реально проходят пороги.
Это и есть настоящий темп обучения системы — сколько экспериментов можно вести
параллельно. Число получаем из данных, а не из предположений.

Два порога:
  - решение на объекте: Σ p_pay ≥ 25 за окно (MDE ≈ 30% при базе 1.4%);
  - A/B-сплит: ≥ 400 эффективных лидов в месяц (Яндекс рекомендует 200 на плечо).
"""

from typing import Any, Dict, List

MIN_EXPECTED_PAYMENTS = 25.0
AB_MIN_EFF_LEADS = 400


def power_report(
    campaign_aggregates: List[Dict[str, Any]],
    min_expected: float = MIN_EXPECTED_PAYMENTS,
    ab_min_leads: int = AB_MIN_EFF_LEADS,
) -> Dict[str, Any]:
    ab_ready = [
        c["campaign_id"]
        for c in campaign_aggregates
        if int(c.get("eff_leads_30d") or 0) >= ab_min_leads
    ]
    return {
        "total": len(campaign_aggregates),
        "pass_decision_threshold": sum(
            1 for c in campaign_aggregates
            if float(c.get("sum_p_pay_30d") or 0.0) >= min_expected
        ),
        "pass_ab_threshold": len(ab_ready),
        "campaigns_ab_ready": sorted(ab_ready),
    }
