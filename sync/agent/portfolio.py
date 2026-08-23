# -*- coding: utf-8 -*-
"""
sync/agent/portfolio.py — Э3.2: единый порог предельной окупаемости и целевые
бюджеты кампаний.

Валюта решения — ожидаемая прибыль (принцип Павла), а не «больше лидов»:
предельный лид кампании ценен ожидаемой выручкой, которую он приносит
(лестница Э2.1: expected_revenue / события eff), и стоит предельную цену из
кривой насыщения Э3.1 (marginal_cpl · (S/S₀)^(1−β)). Оптимум портфеля —
выровнять ПРЕДЕЛЬНУЮ ОКУПАЕМОСТЬ λ = ценность лида / предельная цена лида:
у кого предельный рубль возвращает больше λ — тому долить, у кого меньше —
срезать, пока сумма целевых бюджетов не сойдётся с бюджетом кабинета.

    S*_i(λ) = S₀_i · (value_i / (λ · m₀_i))^(1/(1−β_i)),  β_i < 1

λ ищется бинарным поиском: Σ S*_i(λ) монотонно убывает по λ, решение — где
сумма равна бюджету. Бюджет — ТЕКУЩИЙ расход кабинета за то же окно: Э3.2 —
перенос при том же объёме (проверка суммы на уровне кабинета — прямо в
выходе); план освоения B от Павла подставится сюда же, когда появится.

Шаг зажат в [×0.5, ×1.5] за такт: дальше собственных наблюдений кривая —
экстраполяция, а недельный такт волен повторить шаг. Кампании без ценности
лида (лестница без ступени или без чека) в перенос не входят — их бюджет
закреплён и виден счётчиком, а не молчанием.

Это расчётный слой: запись бюджетов — Э3.3. Вердикт каждого сдвига — шкалой
Э2.3 классом budget_shift; неуверенный сдвиг — не рекомендация.
"""

import math
from typing import Any, Dict, List, Optional, Tuple

from sync.agent.confidence import assess

# Кап шага за такт. Симметричный в лог-смысле он не обязан быть: ×1.5 вверх
# отыгрывается срезанием, ×0.5 вниз — доливом на следующем такте.
MAX_STEP_UP = 1.5
MAX_STEP_DOWN = 0.5
_BISECT_ITERATIONS = 80

# Насколько предельная окупаемость на полу капа должна не дотягивать до λ,
# чтобы кампания стала кандидатом на выключение (Э3.4). Без запаса кандидатом
# была бы любая кампания, чей целевой бюджет упёрся в кап вниз, — а это
# штатная ситуация переноса, за такт-другой она рассасывается. Кандидат —
# кампания, которой не помогает даже урезание вдвое: предельная окупаемость
# на полу всё ещё ниже λ с запасом.
SWITCH_OFF_ROI_SHARE = 0.75


def value_per_lead(ladder_row: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """Ожидаемая выручка на эффективный лид кампании из строки лестницы.

    None — ценность не оценить: нет ступени, нет чека направления
    (expected_revenue не посчитан) или нет самих эффективных лидов в окне.
    Ошибка — лестничная: она уже включает ступень объекта и коэффициенты пула.
    """
    revenue = ladder_row.get("expected_revenue")
    eff = float((ladder_row.get("events_by_step") or {}).get("eff") or 0.0)
    rel = ladder_row.get("rel_error")
    if revenue is None or eff <= 0 or not rel:
        return None
    return {"value": float(revenue) / eff, "rel_error": float(rel)}


def _target_spend(campaign: Dict[str, Any], lam: float) -> float:
    """Целевой бюджет кампании при пороге λ, с капом шага.

    Считается в лог-пространстве: при β → 1 показатель 1/(1−β) взрывается, и
    прямое возведение в степень переполняется задолго до капа. β ≥ 1
    (насыщения нет) — предельная цена с объёмом не растёт, порог внутри капа
    не пересекается: кампания упирается в кап той стороны, где её текущая
    предельная окупаемость относительно λ.
    """
    cost = campaign["cost"]
    log_ratio = math.log(campaign["value"] / (lam * campaign["marginal_cpl"]))
    beta = campaign["beta"]
    if beta >= 1.0:
        return cost * (MAX_STEP_UP if log_ratio > 0 else MAX_STEP_DOWN)
    scaled = log_ratio / (1.0 - beta)
    if scaled >= math.log(MAX_STEP_UP):
        return cost * MAX_STEP_UP
    if scaled <= math.log(MAX_STEP_DOWN):
        return cost * MAX_STEP_DOWN
    return cost * math.exp(scaled)


def solve_threshold(
    campaigns: List[Dict[str, Any]], budget: float,
) -> Tuple[float, Dict[str, float]]:
    """λ и целевые бюджеты, при которых Σ целевых = бюджету.

    Σ S*(λ) монотонно убывает по λ (каждое слагаемое не возрастает), поэтому
    бинарный поиск. Из-за капов сумма кусочно-постоянна: на плато поиск
    останавливается на любом λ плато — целевые бюджеты при этом одни и те же.
    """
    lo, hi = 1e-9, 1e9
    for _ in range(_BISECT_ITERATIONS):
        mid = math.sqrt(lo * hi)  # порог живёт в лог-шкале, как и eps
        total = sum(_target_spend(c, mid) for c in campaigns)
        if total > budget:
            lo = mid
        else:
            hi = mid
    lam = math.sqrt(lo * hi)
    return lam, {c["campaign_id"]: _target_spend(c, lam) for c in campaigns}


def _switch_off_candidate(
    campaign: Dict[str, Any], ratio: float, lam: float, rel: float,
) -> Optional[Dict[str, Any]]:
    """Кандидат на выключение (Э3.4), или None.

    Кандидат — кампания, которой не помогает даже кап вниз: целевой бюджет
    на полу, а предельная окупаемость НА ПОЛУ (вдоль кривой:
    m(S) = m₀·(S/S₀)^(1−β), при β<1 урезание её улучшает) всё ещё ниже λ
    с запасом SWITCH_OFF_ROI_SHARE. Вердикт — классом campaign_state
    (0.97: остановка теряет обучение стратегии, эффект не отматывается);
    гейт уверенности применяет писатель, здесь кандидат только размечается.
    """
    if ratio > MAX_STEP_DOWN + 1e-9:
        return None
    beta = campaign["beta"]
    marginal_at_floor = (campaign["marginal_cpl"]
                         * MAX_STEP_DOWN ** (1.0 - beta) if beta < 1.0
                         else campaign["marginal_cpl"])
    roi_at_floor = campaign["value"] / marginal_at_floor
    roi_share = roi_at_floor / lam
    if roi_share >= SWITCH_OFF_ROI_SHARE:
        return None
    verdict = assess(roi_share, rel, "campaign_state")
    return {
        "roi_at_floor": round(roi_at_floor, 4),
        "roi_share_of_lambda": round(roi_share, 4),
        "p_sign": verdict["p_sign"],
        "confident": verdict["confident"] is True,
    }


def _move_row(campaign: Dict[str, Any], target: float, lam: float) -> Dict[str, Any]:
    """Строка рекомендации: дельта, ожидаемый эффект, вердикт сдвига.

    Ожидаемые лиды — вдоль кривой: leads·((S*/S₀)^β − 1); выручка — через
    ценность лида. Ошибка решения складывается из ошибки ценности (лестница)
    и ошибки предельной цены (Э3.1) — независимые источники.
    """
    cost = campaign["cost"]
    ratio = target / cost
    delta_leads = campaign["leads"] * (ratio ** campaign["beta"] - 1.0)
    rel = math.sqrt(campaign["value_rel_error"] ** 2
                    + campaign["marginal_rel_error"] ** 2)
    verdict = assess(ratio, rel, "budget_shift")
    if abs(ratio - 1.0) < 0.01:
        move = "hold"
    else:
        move = "up" if ratio > 1.0 else "down"
    switch_off = _switch_off_candidate(campaign, ratio, lam, rel)
    return {
        **({"switch_off": switch_off} if switch_off else {}),
        "direction": campaign["direction"],
        "leads_28d": campaign["leads"],
        "cost_28d": round(cost, 2),
        "target_28d": round(target, 2),
        "ratio": round(ratio, 4),
        "move": move,
        "value_per_lead": round(campaign["value"], 2),
        "marginal_cpl": round(campaign["marginal_cpl"], 2),
        "marginal_roi": round(campaign["value"] / campaign["marginal_cpl"], 4),
        "expected_leads_delta": round(delta_leads, 1),
        "expected_revenue_delta": round(delta_leads * campaign["value"], 2),
        "rel_error": round(rel, 4),
        "p_sign": verdict["p_sign"],
        "confident": verdict["confident"] is True,
    }


def portfolio_targets(
    saturation_campaigns: Dict[str, Dict[str, Any]],
    ladder_by_object: Dict[str, Dict[str, Any]],
    login_by_campaign_id: Dict[str, str],
) -> Dict[str, Any]:
    """Целевые бюджеты по кабинетам: порог λ на кабинет, сумма сохраняется.

    Деньги двигаются ВНУТРИ кабинета Директа — общий счёт один на кабинет, и
    сумма обязана сходиться там же. Кампании без привязки к кабинету решаются
    своей группой "unmapped": молчать о них нельзя, но и смешивать их бюджет
    с чьим-то кабинетом — тоже.
    """
    by_login: Dict[str, List[Dict[str, Any]]] = {}
    fixed_by_login: Dict[str, Dict[str, float]] = {}
    no_value = 0
    for campaign_id, curve in saturation_campaigns.items():
        login = login_by_campaign_id.get(str(campaign_id)) or "unmapped"
        value = value_per_lead(ladder_by_object.get(str(campaign_id)) or {})
        if value is None:
            no_value += 1
            fixed = fixed_by_login.setdefault(login, {"campaigns": 0, "cost": 0.0})
            fixed["campaigns"] += 1
            fixed["cost"] += curve["cost_28d"]
            continue
        by_login.setdefault(login, []).append({
            "campaign_id": str(campaign_id),
            "direction": curve.get("direction") or "unknown",
            "cost": float(curve["cost_28d"]),
            "leads": int(curve["leads_28d"]),
            "beta": float(curve["beta"]),
            "marginal_cpl": float(curve["marginal_cpl"]),
            "marginal_rel_error": float(curve["marginal_rel_error"]),
            "value": value["value"],
            "value_rel_error": value["rel_error"],
        })

    accounts: Dict[str, Dict[str, Any]] = {}
    for login, campaigns in sorted(by_login.items()):
        budget = sum(c["cost"] for c in campaigns)
        if budget <= 0:
            continue
        lam, targets = solve_threshold(campaigns, budget)
        moves = {c["campaign_id"]: _move_row(c, targets[c["campaign_id"]], lam)
                 for c in campaigns}
        target_sum = sum(targets.values())
        confident = [m for m in moves.values() if m["confident"] and m["move"] != "hold"]
        switch_off = [m for m in moves.values() if m.get("switch_off")]
        accounts[login] = {
            "lambda": round(lam, 4),
            "budget_28d": round(budget, 2),
            "target_sum_28d": round(target_sum, 2),
            # Невязка суммы — прямая проверка «сумма на уровне кабинета».
            "sum_residual": round(target_sum - budget, 2),
            "campaigns": len(campaigns),
            "fixed": fixed_by_login.get(login, {"campaigns": 0, "cost": 0.0}),
            "moves_up": sum(1 for m in moves.values() if m["move"] == "up"),
            "moves_down": sum(1 for m in moves.values() if m["move"] == "down"),
            "moves_confident": len(confident),
            "switch_off_candidates": len(switch_off),
            "expected_leads_delta": round(
                sum(m["expected_leads_delta"] for m in moves.values()), 1),
            "expected_revenue_delta": round(
                sum(m["expected_revenue_delta"] for m in moves.values()), 2),
            "moves": moves,
        }
    return {
        "campaigns_no_value": no_value,
        "accounts": accounts,
    }


def computed_rows(section: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Строки edu_agent_computed_settings: целевой бюджет по кампаниям.

    value — целевой расход за 28 дней, raw_value — текущий: потребитель Э3.3
    обязан видеть обе стороны переноса, а не только результат. support_n —
    лиды окна (сила текущей точки), rel_error — ошибка решения.
    """
    out: Dict[str, List[Dict[str, Any]]] = {}
    for account in section.get("accounts", {}).values():
        for campaign_id, m in account["moves"].items():
            rows = [{
                "setting_kind": "budget_target",
                "setting_key": "target_28d",
                "value": m["target_28d"],
                "raw_value": m["cost_28d"],
                "support_n": m["leads_28d"],
                "rel_error": m["rel_error"],
            }]
            switch = m.get("switch_off")
            if switch:
                # value — доля предельной окупаемости на полу от λ (<0.75 по
                # построению), raw_value — сама окупаемость. Писатель Э3.4
                # пересчитает вердикт классом campaign_state из этих же чисел.
                rows.append({
                    "setting_kind": "campaign_switch",
                    "setting_key": "suspend",
                    "value": switch["roi_share_of_lambda"],
                    "raw_value": switch["roi_at_floor"],
                    "support_n": m["leads_28d"],
                    "rel_error": m["rel_error"],
                })
            out[campaign_id] = rows
    return out
