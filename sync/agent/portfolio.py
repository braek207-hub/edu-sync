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
from typing import Any, Dict, List, Optional, Set, Tuple

from sync.agent.confidence import assess

# Кап шага за такт. Симметричный в лог-смысле он не обязан быть: ×1.5 вверх
# отыгрывается срезанием, ×0.5 вниз — доливом на следующем такте.
# Доля бюджета кабинета на РАЗВЕДКУ. Без неё кривая насыщения уточняется
# только там, где история уже что-то говорит: кампания с неопределённой
# оценкой такой и остаётся, а механизм год за годом делит деньги по тому, что
# знал вначале. Карман берётся ИЗ бюджета, а не сверх него — инвариант
# «сумма целевых = бюджету кабинета» не меняется.
EXPLORATION_SHARE = 0.07

MAX_STEP_UP = 1.5
MAX_STEP_DOWN = 0.5

# Шаг кампании с β ≥ 1. «Насыщения не видно» — экстраполяционное утверждение
# с самым слабым обоснованием из всей кривой (saturation.BETA_MAX его же и
# зажимает сверху), а прыжок сразу в кап ×1.5 ставил максимальную ставку
# ровно на слабейшую оценку. Шаг умеренный: направление сохраняется,
# недельный такт волен его повторить, когда наблюдения подтвердятся.
BETA_SUPERLINEAR_STEP = 1.15
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
    (насыщения нет) — порог внутри капа не пересекается; шаг в сторону
    текущей предельной окупаемости относительно λ, но умеренный
    (BETA_SUPERLINEAR_STEP), а не сразу в кап: см. комментарий у константы.
    """
    cost = campaign["cost"]
    log_ratio = math.log(campaign["value"] / (lam * campaign["marginal_cpl"]))
    beta = campaign["beta"]
    if beta >= 1.0:
        return cost * (BETA_SUPERLINEAR_STEP if log_ratio > 0
                       else 1.0 / BETA_SUPERLINEAR_STEP)
    scaled = log_ratio / (1.0 - beta)
    if scaled >= math.log(MAX_STEP_UP):
        return cost * MAX_STEP_UP
    if scaled <= math.log(MAX_STEP_DOWN):
        return cost * MAX_STEP_DOWN
    return cost * math.exp(scaled)


def exploration_bonus(
    campaigns: List[Dict[str, Any]], explore_rub: float,
) -> Dict[str, float]:
    """Разведочная надбавка по кампаниям: {campaign_id: ₽}.

    Делится пропорционально НЕЗНАНИЮ — совокупной относительной ошибке
    оценки (ценность лида и предельная цена, независимые источники), взвешенной
    расходом кампании: узнать про кампанию, которая тратит много и понята
    плохо, ценнее всего. Кандидаты на выключение исключены: разведка на том,
    что закрывается, — деньги на изучение уходящего.

    Совсем нет кого изучать (все кандидаты на выключение или ошибки нулевые) —
    пустой словарь: карман вернётся в общий солвер, а не растворится.
    """
    weights: Dict[str, float] = {}
    for campaign in campaigns:
        if campaign.get("switch_off"):
            continue
        rel = math.sqrt(float(campaign.get("value_rel_error") or 0.0) ** 2
                        + float(campaign.get("marginal_rel_error") or 0.0) ** 2)
        weight = rel * float(campaign.get("cost") or 0.0)
        if weight > 0:
            weights[str(campaign["campaign_id"])] = weight
    total = sum(weights.values())
    if total <= 0 or explore_rub <= 0:
        return {}
    return {cid: explore_rub * w / total for cid, w in weights.items()}


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

    Уверенность — по ЭКОНОМИЧЕСКОЙ гипотезе value против λ·marginal, тем же
    инвариантом, что у кандидата на выключение: числитель assess и его ошибка
    описывают одну и ту же величину. Старый assess(target/cost) мерил готовый
    шаг: при β → 1 показатель 1/(1−β) раздувал шаг до капа, и p_sign выходил
    ~0.98 там, где экономическая разница тонула в шуме, — раздутая
    уверенность двигала деньги по слепой истории (аудит 2026-08-23, C2).
    """
    cost = campaign["cost"]
    ratio = target / cost
    delta_leads = campaign["leads"] * (ratio ** campaign["beta"] - 1.0)
    rel = math.sqrt(campaign["value_rel_error"] ** 2
                    + campaign["marginal_rel_error"] ** 2)
    roi_ratio = campaign["value"] / (lam * campaign["marginal_cpl"])
    verdict = assess(roi_ratio, rel, "budget_shift")
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
        "marginal_roi_vs_lambda": round(roi_ratio, 4),
        "expected_leads_delta": round(delta_leads, 1),
        "expected_revenue_delta": round(delta_leads * campaign["value"], 2),
        "rel_error": round(rel, 4),
        "p_sign": verdict["p_sign"],
        "confident": verdict["confident"] is True,
    }


def _apply_exploration(
    targets: Dict[str, float], cost_by_id: Dict[str, float],
    explore_rub: float, campaigns: List[Dict[str, Any]],
) -> Tuple[Dict[str, float], Dict[str, float]]:
    """Цели с изъятым и перераспределённым карманом разведки.

    Карман берётся ТОЛЬКО из запаса цели до нижнего капа шага: кампания,
    уже стоящая на полу (×MAX_STEP_DOWN), не должна проседать глубже — кап
    шага держится и при разведке. Столько же и раздаётся, поэтому сумма
    целевых по кабинету не меняется. Раздача ограничена верхним капом по той
    же причине; невыданный остаток возвращается источникам пропорционально
    изъятому, а не растворяется.
    """
    floors = {cid: cost_by_id.get(cid, 0.0) * MAX_STEP_DOWN for cid in targets}
    headroom = {cid: max(0.0, targets[cid] - floors[cid]) for cid in targets}
    total_headroom = sum(headroom.values())
    taken = min(float(explore_rub), total_headroom)
    if taken <= 0:
        return targets, {}

    withdrawn = {cid: taken * h / total_headroom for cid, h in headroom.items()}
    bonus = exploration_bonus(campaigns, taken)
    if not bonus:
        return targets, {}

    ceilings = {cid: cost_by_id.get(cid, 0.0) * MAX_STEP_UP for cid in targets}
    out = {cid: targets[cid] - withdrawn.get(cid, 0.0) for cid in targets}
    unspent = 0.0
    for cid, extra in bonus.items():
        room = max(0.0, ceilings.get(cid, float("inf")) - out.get(cid, 0.0))
        given = min(extra, room)
        out[cid] = out.get(cid, 0.0) + given
        unspent += extra - given
    if unspent > 0 and taken > 0:
        # Возврат тем, у кого изъяли: сумма кабинета обязана сойтись.
        for cid, amount in withdrawn.items():
            out[cid] += unspent * amount / taken
    return out, bonus


def portfolio_targets(
    saturation_campaigns: Dict[str, Dict[str, Any]],
    ladder_by_object: Dict[str, Dict[str, Any]],
    login_by_campaign_id: Dict[str, str],
    holdout_ids: Optional[Set[str]] = None,
    explore_share: float = EXPLORATION_SHARE,
) -> Dict[str, Any]:
    """Целевые бюджеты по кабинетам: порог λ на кабинет, сумма сохраняется.

    Деньги двигаются ВНУТРИ кабинета Директа — общий счёт один на кабинет, и
    сумма обязана сходиться там же. Кампании без привязки к кабинету решаются
    своей группой "unmapped": молчать о них нельзя, но и смешивать их бюджет
    с чьим-то кабинетом — тоже.

    holdout_ids — заповедник: кампании, которых агент не трогает по
    определению (agent_e1 блокирует по ним действия). В солвер они не идут.
    Держать их внутри значило бы задавать порог λ ограничением «сумма целевых
    равна сумме текущих», часть которой никогда не сдвинется: сумма сходится
    фиктивно, а сам порог отчасти задают неподвижные кампании. Их бюджет
    виден отдельной строкой отчёта, как и у кампаний без ценности лида.
    """
    holdout_ids = {str(h) for h in (holdout_ids or set())}
    by_login: Dict[str, List[Dict[str, Any]]] = {}
    fixed_by_login: Dict[str, Dict[str, float]] = {}
    holdout_by_login: Dict[str, Dict[str, float]] = {}
    no_value = 0
    for campaign_id, curve in saturation_campaigns.items():
        login = login_by_campaign_id.get(str(campaign_id)) or "unmapped"
        if str(campaign_id) in holdout_ids:
            guarded = holdout_by_login.setdefault(
                login, {"campaigns": 0, "cost": 0.0})
            guarded["campaigns"] += 1
            guarded["cost"] += float(curve["cost_28d"])
            continue
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
        # Порог и вердикты считаются на ПОЛНОМ бюджете: карман не должен
        # двигать λ, иначе кампания у пола капа становится кандидатом на
        # выключение только потому, что часть денег ушла на разведку.
        lam, targets = solve_threshold(campaigns, budget)
        preliminary = {c["campaign_id"]: _move_row(c, targets[c["campaign_id"]], lam)
                       for c in campaigns}
        # Карман изымается ПОСЛЕ решения — пропорционально у всех — и уходит
        # туда, где оценка хуже всего. Сумма целевых при этом не меняется:
        # (1−share)·Σцелей + share·бюджет = бюджет.
        cost_by_id = {c["campaign_id"]: c["cost"] for c in campaigns}
        targets, bonus = _apply_exploration(
            targets, cost_by_id, budget * EXPLORATION_SHARE,
            [{**c, "switch_off": bool(preliminary[c["campaign_id"]].get("switch_off"))}
             for c in campaigns])
        moves = {c["campaign_id"]: _move_row(c, targets[c["campaign_id"]], lam)
                 for c in campaigns}
        target_sum = sum(targets.values())
        confident = [m for m in moves.values() if m["confident"] and m["move"] != "hold"]
        switch_off = [m for m in moves.values() if m.get("switch_off")]
        accounts[login] = {
            "lambda": round(lam, 4),
            # λ — окупаемость предельного рубля кабинета. λ < 1: предельный
            # рубль возвращает меньше рубля ожидаемой выручки, перенос лишь
            # выравнивает убыточность. Само по себе это не команда резать
            # бюджет — план освоения B задаёт Павел, — но состояние обязано
            # быть видно флагом, а не прятаться в числе (аудит 2026-08-23, C6).
            "lambda_breakeven": bool(lam >= 1.0),
            "budget_28d": round(budget, 2),
            # Сколько денег кабинета ушло на разведку и кому: без этой строки
            # надбавка неотличима от решения солвера.
            "exploration": {
                "share": explore_share if bonus else 0.0,
                "rub": round(sum(bonus.values()), 2),
                "campaigns": len(bonus),
            },
            "target_sum_28d": round(target_sum, 2),
            # Невязка суммы — прямая проверка «сумма на уровне кабинета».
            "sum_residual": round(target_sum - budget, 2),
            "campaigns": len(campaigns),
            "fixed": fixed_by_login.get(login, {"campaigns": 0, "cost": 0.0}),
            # Заповедник: в солвере не участвует, но его расход обязан быть
            # виден — иначе «бюджет кабинета» в отчёте меньше настоящего без
            # объяснения.
            "holdout": holdout_by_login.get(login, {"campaigns": 0, "cost": 0.0}),
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
        "accounts_below_breakeven": sorted(
            login for login, acc in accounts.items()
            if not acc["lambda_breakeven"]),
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
            }, {
                # Экономическое отношение value/(λ·marginal) — отдельной
                # строкой: писатель Э3.3 гейтует уверенность заново, и без
                # него он судил бы по target/cost, то есть повторял бы
                # раздутый p_sign, снятый здесь. raw_value — λ кабинета.
                "setting_kind": "budget_target",
                "setting_key": "roi_vs_lambda",
                "value": m["marginal_roi_vs_lambda"],
                "raw_value": account["lambda"],
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
