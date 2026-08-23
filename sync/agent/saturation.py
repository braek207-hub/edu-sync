# -*- coding: utf-8 -*-
"""
sync/agent/saturation.py — Э3.1: кривые насыщения и предельная цена лида.

Терцильная картинка точки отсчёта («spo недолито») конфаундится сезоном:
высокие недели — это сезон, когда и конверсия выше. Поэтому оба источника
кривой — DiD против остальных кампаний кабинета за тот же период, где сезон
вычитается сам:

  1. Квазиэксперименты Э2.4 (mining.py) — 14-дневные окна вокруг найденных
     скачков бюджета. Точные, но редкие.
  2. Пары соседних недель — та же DiD-оценка на недельных агрегатах: неделя
     «до» и «после» для кампании, чей недельный бюджет скакнул, против всех
     остальных кампаний за те же две недели. Мельче и шумнее, но их много.
     Пара, внутрь которой попал уже посчитанный квазиэксперимент кампании,
     пропускается — иначе один и тот же скачок вошёл бы в свод дважды и
     фиктивно сузил ошибку.

Оба источника дают наблюдения одной величины — эластичности цены лида по
бюджету eps = DiD-эффект / ln(бюджет после/до) — и сводятся обратновзвешенно
(history.combine) с частичным пулингом: свои наблюдения кампании плюс пул её
направления БЕЗ неё самой (leave-one-out — иначе свои наблюдения вошли бы
дважды); совсем без данных — пул кабинета.

Из eps — кривая насыщения степенного вида: лиды = a · бюджет^β, где β = 1 − eps.
Тогда предельная цена лида на текущем объёме считается в одно действие:

    marginal_cpl = средний CPL / β

а вдоль кривой marginal_cpl(S) = marginal_cpl · (S/S₀)^(1−β). Эти два числа
на кампанию (β и предельная цена) — весь вход Э3.2: единый порог предельной
цены и перенос бюджетов сравнивают именно их.

Класс надёжности B удержан: DiD чистит сезон, но не причину скачка (трогали
то, что болело), поэтому кривая — приоритет гипотез, а не приказ.
"""

import math
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sync.agent.confidence import assess
from sync.agent.history import MIN_LOG_JUMP, combine, elasticity
from sync.agent.mining import did_effect, did_rel_error

# Границы β. Ниже 0.15 предельная цена взлетает в десятки средних CPL — на
# наших ошибках (rel_error свода ≥ 0.2) такая крутизна неотличима от шума и
# читалась бы как приказ «немедленно всё срезать». Выше 1.5 — «возрастающая
# отдача» сильнее, чем DiD способен доказать. Зажим виден в отчёте флагом.
BETA_MIN = 0.15
BETA_MAX = 1.5
# Текущая точка кривой — последние 28 зрелых дней: месяц сглаживает недельный
# ритм, но не тянет прошлый сезон.
RECENT_DAYS = 28


def _week_start(day: str) -> str:
    d = date.fromisoformat(day[:10])
    return (d - timedelta(days=d.weekday())).isoformat()


def weekly_pair_observations(
    facts: List[Dict[str, Any]],
    mature_through: str,
    quasi_dates_by_campaign: Optional[Dict[str, List[str]]] = None,
) -> Tuple[Dict[str, List[Dict[str, float]]], Dict[str, int]]:
    """Наблюдения эластичности из пар соседних недель, по кампаниям.

    Неделя календарная (пн–вс) и только ПОЛНАЯ и ЗРЕЛАЯ: воскресенье не позже
    границы зрелости CRM — эффективные лиды свежих дней ещё едут, и хвостовая
    неделя занизила бы лиды сразу всем, а DiD этого не простит на разных
    объёмах. Первая неделя окна фактов отбрасывается той же логикой: она
    обрезана началом окна.
    """
    quasi_dates_by_campaign = quasi_dates_by_campaign or {}
    if not facts:
        return {}, {"pairs_checked": 0, "pairs_used": 0, "pairs_quasi_overlap": 0}

    min_date = min(str(f["fact_date"])[:10] for f in facts)
    # Первый понедельник не раньше начала окна и последнее зрелое воскресенье.
    first_monday = _week_start(min_date)
    if first_monday < min_date:
        first_monday = (date.fromisoformat(first_monday) + timedelta(days=7)).isoformat()

    weekly: Dict[str, Dict[str, Dict[str, float]]] = {}
    totals: Dict[str, Dict[str, float]] = {}
    for f in facts:
        day = str(f["fact_date"])[:10]
        week = _week_start(day)
        week_end = (date.fromisoformat(week) + timedelta(days=6)).isoformat()
        if week < first_monday or week_end > mature_through:
            continue
        cost = float(f.get("cost") or 0.0)
        leads = int(f.get("eff_leads") or 0)
        camp = weekly.setdefault(str(f["campaign_id"]), {})
        cell = camp.setdefault(week, {"cost": 0.0, "leads": 0})
        cell["cost"] += cost
        cell["leads"] += leads
        total = totals.setdefault(week, {"cost": 0.0, "leads": 0})
        total["cost"] += cost
        total["leads"] += leads

    out: Dict[str, List[Dict[str, float]]] = {}
    stats = {"pairs_checked": 0, "pairs_used": 0, "pairs_quasi_overlap": 0}
    for campaign_id, by_week in sorted(weekly.items()):
        weeks = sorted(by_week)
        quasi_dates = quasi_dates_by_campaign.get(campaign_id, [])
        for w1, w2 in zip(weeks, weeks[1:]):
            if (date.fromisoformat(w2) - date.fromisoformat(w1)).days != 7:
                continue
            t1, t2 = by_week[w1], by_week[w2]
            if t1["cost"] <= 0 or t2["cost"] <= 0:
                continue
            stats["pairs_checked"] += 1
            log_jump = math.log(t2["cost"] / t1["cost"])
            if abs(log_jump) < MIN_LOG_JUMP:
                continue
            pair_end = (date.fromisoformat(w2) + timedelta(days=6)).isoformat()
            if any(w1 <= d <= pair_end for d in quasi_dates):
                stats["pairs_quasi_overlap"] += 1
                continue
            # Контроль = кабинет минус кампания за те же две недели.
            c1 = {"cost": totals[w1]["cost"] - t1["cost"],
                  "leads": totals[w1]["leads"] - t1["leads"]}
            c2 = {"cost": totals[w2]["cost"] - t2["cost"],
                  "leads": totals[w2]["leads"] - t2["leads"]}
            cells = (t1, t2, c1, c2)
            if any(c["leads"] <= 0 for c in cells) or c1["cost"] <= 0 or c2["cost"] <= 0:
                # Окно без лидов конечной цены лида не даёт (см. mining).
                continue
            rel = did_rel_error(*(c["leads"] for c in cells))
            measured = did_effect(
                t1["cost"] / t1["leads"], t2["cost"] / t2["leads"],
                c1["cost"] / c1["leads"], c2["cost"] / c2["leads"],
                rel,
            )
            if measured["effect"] is None:
                continue
            stats["pairs_used"] += 1
            out.setdefault(campaign_id, []).append({
                "eps": float(measured["effect"]) / log_jump,
                "rel_error": rel / abs(log_jump),
            })
    return out, stats


def _pooled_eps(
    own: List[Dict[str, float]],
    direction_pool: List[Dict[str, float]],
    account_pool: List[Dict[str, float]],
) -> Tuple[Optional[Dict[str, float]], str]:
    """Свод eps кампании с частичным пулингом: свои наблюдения + пул направления
    без неё самой одним «наблюдением»; совсем без своих — направление, затем
    кабинет. Источник возвращается наружу: в отчёте обязано быть видно, чьи
    это числа — самой кампании или её соседей."""
    prior = combine(direction_pool)
    if own:
        merged = combine(own + ([prior] if prior else []))
        return merged, ("campaign+direction" if prior else "campaign")
    if prior:
        return prior, "direction"
    account = combine(account_pool)
    return account, ("account" if account else "none")


def _curve(eps: Dict[str, float], cost: float, leads: int) -> Dict[str, Any]:
    """Точка кривой: β, предельная цена и её ошибка из eps и текущего объёма.

    Ошибка предельной цены складывается из пуассоновской ошибки среднего CPL
    (1/√лидов) и ошибки β (σ_β = σ_eps, β = 1 − eps) — независимые источники,
    дисперсии складываются.
    """
    beta_raw = 1.0 - eps["eps"]
    beta = min(max(beta_raw, BETA_MIN), BETA_MAX)
    cpl = cost / leads
    marginal = cpl / beta
    rel = math.sqrt(1.0 / leads + (eps["rel_error"] / beta) ** 2)
    verdict = assess(math.exp(eps["eps"]), eps["rel_error"], "budget_shift")
    if verdict["confident"] is True:
        label = "насыщается" if eps["eps"] > 0 else "не насыщена"
    else:
        label = "неопределённо"
    return {
        "eps": round(eps["eps"], 4),
        "eps_rel_error": round(eps["rel_error"], 4),
        "beta": round(beta, 4),
        "beta_clamped": beta != beta_raw,
        "observations": int(eps.get("n") or 0),
        "cost_28d": round(cost, 2),
        "leads_28d": leads,
        "cpl_28d": round(cpl, 2),
        "marginal_cpl": round(marginal, 2),
        "marginal_rel_error": round(rel, 4),
        "p_sign": verdict["p_sign"],
        "verdict": label,
    }


def saturation_curves(
    facts: List[Dict[str, Any]],
    quasi_experiments: List[Dict[str, Any]],
    direction_by_campaign: Dict[str, Optional[str]],
    mature_through: str,
) -> Dict[str, Any]:
    """Кривые насыщения по кампаниям и направлениям.

    Возвращает отчётную секцию; строки для edu_agent_computed_settings из неё
    собирает computed_rows(). Кампании без расхода или лидов в свежем зрелом
    окне кривой не получают — предельную цену не к чему прикладывать — и
    перечисляются счётчиком, а не молчанием.
    """
    quasi_dates: Dict[str, List[str]] = {}
    quasi_obs: Dict[str, List[Dict[str, float]]] = {}
    for exp in quasi_experiments:
        campaign_id = str(exp.get("object_id"))
        started = str(exp.get("started_on") or "")[:10]
        if started:
            quasi_dates.setdefault(campaign_id, []).append(started)
        obs = elasticity(exp)
        if obs is not None:
            quasi_obs.setdefault(campaign_id, []).append(obs)

    weekly_obs, weekly_stats = weekly_pair_observations(facts, mature_through, quasi_dates)

    all_obs: Dict[str, List[Dict[str, float]]] = {}
    for campaign_id in set(quasi_obs) | set(weekly_obs):
        all_obs[campaign_id] = quasi_obs.get(campaign_id, []) + weekly_obs.get(campaign_id, [])

    by_direction_obs: Dict[str, List[Dict[str, float]]] = {}
    for campaign_id, observations in all_obs.items():
        direction = direction_by_campaign.get(campaign_id) or "unknown"
        by_direction_obs.setdefault(str(direction), []).extend(observations)
    account_pool = [o for obs in all_obs.values() for o in obs]

    # Текущая точка: последние RECENT_DAYS зрелых дней.
    recent_from = (date.fromisoformat(mature_through) - timedelta(days=RECENT_DAYS - 1)).isoformat()
    spend: Dict[str, Dict[str, float]] = {}
    for f in facts:
        day = str(f["fact_date"])[:10]
        if not (recent_from <= day <= mature_through):
            continue
        campaign_id = str(f["campaign_id"])
        cell = spend.setdefault(campaign_id, {"cost": 0.0, "leads": 0})
        cell["cost"] += float(f.get("cost") or 0.0)
        cell["leads"] += int(f.get("eff_leads") or 0)

    campaigns: Dict[str, Dict[str, Any]] = {}
    # Кампания с наблюдениями, но без единой строки в свежем окне, — тоже
    # «нет объёма»: иначе она исчезала бы из отчёта молча.
    no_recent_volume = sum(1 for cid in all_obs if cid not in spend)
    no_signal = 0
    for campaign_id, cell in sorted(spend.items()):
        if cell["cost"] <= 0 or cell["leads"] <= 0:
            no_recent_volume += 1
            continue
        direction = str(direction_by_campaign.get(campaign_id) or "unknown")
        own = all_obs.get(campaign_id, [])
        direction_pool = [o for cid, obs in all_obs.items()
                          if cid != campaign_id
                          and str(direction_by_campaign.get(cid) or "unknown") == direction
                          for o in obs]
        pooled, source = _pooled_eps(own, direction_pool, account_pool)
        if pooled is None:
            no_signal += 1
            continue
        campaigns[campaign_id] = {
            **_curve(pooled, cell["cost"], cell["leads"]),
            "direction": direction,
            "eps_source": source,
        }

    directions: Dict[str, Dict[str, Any]] = {}
    dir_spend: Dict[str, Dict[str, float]] = {}
    for campaign_id, cell in spend.items():
        direction = str(direction_by_campaign.get(campaign_id) or "unknown")
        agg = dir_spend.setdefault(direction, {"cost": 0.0, "leads": 0})
        agg["cost"] += cell["cost"]
        agg["leads"] += cell["leads"]
    for direction, agg in sorted(dir_spend.items()):
        pooled = combine(by_direction_obs.get(direction, []))
        if pooled is None or agg["cost"] <= 0 or agg["leads"] <= 0:
            continue
        directions[direction] = _curve(pooled, agg["cost"], agg["leads"])

    return {
        "mature_through": mature_through,
        "recent_window": [recent_from, mature_through],
        "weekly_pairs": weekly_stats,
        "quasi_observations": sum(len(v) for v in quasi_obs.values()),
        "campaigns_with_curve": len(campaigns),
        "campaigns_no_recent_volume": no_recent_volume,
        "campaigns_no_signal": no_signal,
        "campaigns": campaigns,
        "directions": directions,
    }


def computed_rows(section: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Строки edu_agent_computed_settings из секции кривых, по object_id.

    Ключей два на объект: beta (raw_value = eps, rel_error = σ_eps) и
    marginal_cpl (raw_value = средний CPL, rel_error — ошибка предельной
    цены). support_n у beta — число наблюдений эластичности, у marginal_cpl —
    лиды свежего окна: у двух чисел разные источники силы, и потребитель Э3.2
    обязан видеть оба.
    """
    out: Dict[str, List[Dict[str, Any]]] = {}
    for object_id, row in section.get("campaigns", {}).items():
        out[str(object_id)] = [
            {"setting_kind": "saturation", "setting_key": "beta",
             "value": row["beta"], "raw_value": row["eps"],
             "support_n": row["observations"], "rel_error": row["eps_rel_error"]},
            {"setting_kind": "saturation", "setting_key": "marginal_cpl",
             "value": row["marginal_cpl"], "raw_value": row["cpl_28d"],
             "support_n": row["leads_28d"], "rel_error": row["marginal_rel_error"]},
        ]
    return out
