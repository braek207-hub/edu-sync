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


# Стратегии, у которых цель CPA вообще есть. Остальные (максимум кликов,
# ручные, выключенные) этим рычагом не управляются — молчать о них нельзя,
# но и подставлять им цель тоже.
TCPA_STRATEGIES = ("AVERAGE_CPA", "AVERAGE_CPA_MULTIPLE_GOALS")


def _search_strategy(settings: Dict[str, Any]) -> Dict[str, Any]:
    strategy = (settings or {}).get("strategy") or {}
    return (strategy.get("search") or {}) if isinstance(strategy, dict) else {}


def build_inputs(
    facts: List[Dict[str, Any]],
    curves: Dict[str, Dict[str, Any]],
    settings_by_campaign: Dict[str, Dict[str, Any]],
    window_from: str,
    window_to: str,
) -> List[Dict[str, Any]]:
    """Вход рычага: витрина фактов × кривая насыщения × цель из кабинета.

    Окно — ЗРЕЛОЕ: выручка приписана дате создания лида, и свежие дни ещё не
    дозрели (тот же довод, что у лестницы). Кампании на стратегиях без цели
    CPA не попадают сюда вовсе: рычага у них нет.
    """
    totals: Dict[str, Dict[str, float]] = {}
    for row in facts:
        day = str(row.get("fact_date"))[:10]
        if not (window_from <= day <= window_to):
            continue
        slot = totals.setdefault(str(row["campaign_id"]),
                                 {"cost": 0.0, "conversions": 0.0, "revenue": 0.0})
        slot["cost"] += float(row.get("cost") or 0.0)
        slot["conversions"] += float(row.get("conversions") or 0.0)
        slot["revenue"] += float(row.get("revenue") or 0.0)

    out: List[Dict[str, Any]] = []
    for campaign_id, slot in sorted(totals.items()):
        search = _search_strategy(settings_by_campaign.get(campaign_id) or {})
        if str(search.get("biddingStrategyType")) not in TCPA_STRATEGIES:
            continue
        curve = curves.get(campaign_id) or {}
        out.append({
            "campaign_id": campaign_id,
            "cost": slot["cost"],
            "conversions": slot["conversions"],
            "revenue": slot["revenue"],
            "beta": float(curve.get("beta") or 1.0),
            # Ошибка предельной цены кривой — та же неопределённость, что
            # входит множителем β в допустимый CPA.
            "value_rel_error": float(curve.get("marginal_rel_error") or 0.0),
            "tcpa_current": float(search.get("targetCpa") or 0.0),
        })
    return out


def computed_rows(section: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Строки edu_agent_computed_settings: целевой CPA по кампаниям.

    Две строки на кампанию, как у бюджета: сама цель (value — новая,
    raw_value — стоящая в кабинете) и экономическое отношение, по которому
    писатель Э3.5 заново гейтует уверенность.
    """
    out: Dict[str, List[Dict[str, Any]]] = {}
    for campaign_id, row in (section.get("targets") or {}).items():
        out[campaign_id] = [{
            "setting_kind": "tcpa_target",
            "setting_key": "target",
            "value": round(float(row["target"]), 2),
            "raw_value": float(row["tcpa_current"]),
            "support_n": int(row["conversions"]),
            "rel_error": row["rel_error"],
        }, {
            "setting_kind": "tcpa_target",
            "setting_key": "roi_vs_target",
            "value": row["roi_vs_target"],
            "raw_value": row["cpa_fact"],
            "support_n": int(row["conversions"]),
            "rel_error": row["rel_error"],
        }]
    return out
