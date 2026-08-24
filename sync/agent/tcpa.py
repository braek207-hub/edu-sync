# -*- coding: utf-8 -*-
"""
sync/agent/tcpa.py — Э3.5: экономически допустимая цель CPA.

Второй рычаг «вверх» и единственный, который работает у большинства кампаний.
Недельный лимит связывает расход только там, где кампания в него упирается
(замер кабинета: 9 из 62), у остальных лимит висит декорацией, и поднять их
расход нечем — кроме цели CPA автостратегии.

Валюта рычага — КОНВЕРСИЯ ЦЕЛИ ДИРЕКТА, а не эффективный лид CRM и не оплата:
цель в кабинете назначается именно за неё (страница «Спасибо»). Коэффициент
«конверсия → лид → оплата» у кампаний разный, поэтому расчёт целиком ведётся
в конверсиях, а деньги входят через ценность конверсии.

Три величины и их смысл:

  * ЦЕННОСТЬ КОНВЕРСИИ — выручка зрелой когорты, делённая на конверсии того же
    окна. Не средний чек и не ценность лида: у одной конверсии цели может быть
    доля лида, а у лида — доля оплаты.
  * ДОПУСТИМЫЙ ПРЕДЕЛЬНЫЙ CPA — ценность × β / требуемая окупаемость.
    Множитель β (кривая насыщения, Э3.1) переводит среднюю ценность в
    предельную: добирая объём, кампания добирает конверсии дешевле среднего
    по качеству, и платить за них по средней ценности значит переливать.
  * НЕДОДЕРЖАНИЕ ЦЕЛИ — во сколько раз фактический CPA выше поставленной цели.
    На замере кабинета (июнь–июль 2026) факт выше цели почти везде: цель 1200 →
    факт 1687, цель 1000 → факт 2259. Ставить целью сам допустимый CPA значит
    получить факт в полтора раза выше него, поэтому цель делится на
    недодержание — калибровка по самой кампании, а не общая константа.

Уверенность меряется в ЭКОНОМИЧЕСКОЙ гипотезе «допустимый предельный CPA выше
(ниже) фактического», а не в размере шага — тот же инвариант, что у бюджетного
рычага после аудита 2026-08-23 (C2): числитель p_sign и его ошибка обязаны
описывать одну величину.

Модуль чистый: ни БД, ни дат — только счётчики, которые собрал вызывающий.
"""

import math
from typing import Any, Dict, List, Optional

from sync.agent.confidence import assess

# Требуемая окупаемость предельного рубля. По умолчанию 1.0 — безубыточность:
# план освоения задаёт человек, а механизм обязан лишь не уходить в минус.
# Контрактную ×2 вызывающий передаёт явно.
DEFAULT_TARGET_ROMI = 1.0

# Минимум конверсий окна, ниже которого цена конверсии — шум: 1/√25 = 20 %
# относительной ошибки, тот же порог, что у ступеней лестницы.
MIN_CONVERSIONS = 25.0

# Границы недодержания. Ниже 1.0 — кампания держит цель с запасом (факт ниже
# цели), и делить на него значит задирать цель на ровном месте; выше 3 —
# стратегия целью не управляется вовсе, и калибровка по ней бессмысленна.
SLIPPAGE_MIN = 1.0
SLIPPAGE_MAX = 3.0

NO_CONVERSIONS_REASON = (
    f"конверсий цели меньше {int(MIN_CONVERSIONS)} за окно — цена конверсии "
    "неотличима от шума, цель не пересчитывается"
)
NO_VALUE_REASON = (
    "выручки зрелой когорты нет — ценность конверсии не посчитать, "
    "экономически допустимая цель неизвестна"
)
NO_CURRENT_REASON = "у кампании не стоит цель CPA — рычагу не от чего отсчитывать"


def tcpa_target(campaign: Dict[str, Any],
                target_romi: float = DEFAULT_TARGET_ROMI) -> Dict[str, Any]:
    """Экономически допустимая цель CPA одной кампании.

    campaign: cost, conversions, revenue (зрелая когорта), beta,
    value_rel_error, tcpa_current.
    """
    out: Dict[str, Any] = {
        "campaign_id": str(campaign.get("campaign_id")),
        "tcpa_current": float(campaign.get("tcpa_current") or 0.0),
        "conversions": float(campaign.get("conversions") or 0.0),
        "target": None,
        "reason": "",
    }
    conversions = out["conversions"]
    cost = float(campaign.get("cost") or 0.0)
    revenue = float(campaign.get("revenue") or 0.0)
    current = out["tcpa_current"]

    if conversions < MIN_CONVERSIONS or cost <= 0:
        out["reason"] = NO_CONVERSIONS_REASON
        return out
    if revenue <= 0:
        out["reason"] = NO_VALUE_REASON
        return out
    if current <= 0:
        out["reason"] = NO_CURRENT_REASON
        return out

    beta = float(campaign.get("beta") or 1.0)
    cpa_fact = cost / conversions
    value_per_conversion = revenue / conversions
    allowed_marginal = value_per_conversion * beta / float(target_romi)
    # Недодержание считается по самой кампании и зажимается: см. границы выше.
    slippage = min(max(cpa_fact / current, SLIPPAGE_MIN), SLIPPAGE_MAX)
    target = allowed_marginal / slippage

    # Ошибка решения: ценность (лестница) и счёт конверсий — независимые
    # источники. β входит множителем, его ошибку вызывающий передаёт в
    # value_rel_error, если она известна отдельно.
    rel = math.sqrt(float(campaign.get("value_rel_error") or 0.0) ** 2
                    + 1.0 / conversions)
    roi_vs_target = allowed_marginal / cpa_fact
    verdict = assess(roi_vs_target, rel, "budget_shift")

    out.update({
        "cpa_fact": round(cpa_fact, 2),
        "value_per_conversion": value_per_conversion,
        "allowed_marginal_cpa": allowed_marginal,
        "slippage": round(slippage, 4),
        "target": target,
        "ratio": round(target / current, 4),
        "roi_vs_target": round(roi_vs_target, 4),
        "rel_error": round(rel, 4),
        "p_sign": verdict["p_sign"],
        "confident": verdict["confident"],
        "move": "up" if target > current else ("down" if target < current else "hold"),
    })
    return out


def tcpa_targets(campaigns: List[Dict[str, Any]],
                 target_romi: float = DEFAULT_TARGET_ROMI) -> Dict[str, Any]:
    """Целевые CPA по кампаниям + счётчики молчания для отчёта."""
    targets: Dict[str, Dict[str, Any]] = {}
    no_target = 0
    reasons: List[Dict[str, str]] = []
    for campaign in campaigns:
        row = tcpa_target(campaign, target_romi=target_romi)
        if row["target"] is None:
            no_target += 1
            reasons.append({"campaign_id": row["campaign_id"],
                            "reason": row["reason"]})
            continue
        targets[row["campaign_id"]] = row
    return {
        "target_romi": float(target_romi),
        "targets": targets,
        "no_target": no_target,
        "no_target_reasons": reasons,
        "moves_up": sum(1 for r in targets.values() if r["move"] == "up"),
        "moves_down": sum(1 for r in targets.values() if r["move"] == "down"),
        "moves_confident": sum(1 for r in targets.values()
                               if r["confident"] and r["move"] != "hold"),
    }
