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
сумма равна бюджету. Бюджет кабинета — текущий расход за то же окно ПЛЮС
рост, когда предельная окупаемость выше цели с запасом и кабинету есть куда
потратить сегодня (account_budget). Потолок роста — месячный план освоения из
панели настроек; пока он пуст, рост только предлагается числом. Инвариант
«сумма целевых = бюджету кабинета» от этого не меняется — меняется сам
бюджет, и разница названа явно (growth_rub).

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
from sync.agent.tcpa import DEFAULT_TARGET_ROMI
# Признак «лимит связывает расход» задан рычагом записи, и второго определения
# у него быть не должно: разойдись они — расчёт поднимал бы вес кампании,
# которой писатель всё равно откажет. Поэтому и порог, и единицы берутся
# оттуда, а не переписываются здесь.
from sync.agent.writer.budget import (BINDING_SHARE, MAX_WRITE_STEP, VAT,
                                      WEEKS_IN_WINDOW)
from sync.agent.writer.learning import BUDGET_SAFE_DELTA

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

# Расширенный кап шага вверх. Обычный ×1.5 стоит на том, что дальше кривая —
# экстраполяция. Но когда недобор трафика доказан замером (headroom.py),
# кривая не спорит (β<1), предельный рубль возвращает в полтора раза больше
# порога кабинета и текущий лимит СВЯЗЫВАЕТ расход — экстраполяции нет:
# деньги идут в показы, которые уже существуют и сейчас достаются
# конкурентам. Шаг ×2 сбрасывает обучение стратегии (он больше ±20 %) и
# потому сам себя ограничивает кулдауном в 14 дней (writer/budget.py).
BIG_STEP_UP = 2.0
BIG_STEP_ROI_MARGIN = 1.5

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

# Шаг роста ОБЩЕЙ суммы кабинета за такт. Число не своё: это тот же порог, за
# которым правка ограничения расхода перезапускает обучение стратегии
# (writer/learning.BUDGET_SAFE_DELTA). Прибавку раскладывает по кампаниям тот
# же солвер, и держать её в той же границе значит не платить за рост
# переобучением всего кабинета.
ACCOUNT_GROWTH_STEP = BUDGET_SAFE_DELTA

# Запас предельной окупаемости, при котором есть смысл доливать сверху. Ровно
# на цели растить нечего: предельный рубль там уже равен порогу, и прибавка
# сдвинет кабинет за него — кривая насыщения на то и кривая.
GROWTH_LAMBDA_MARGIN = 1.2

# Потолок владелец называет за МЕСЯЦ, а солвер считает окном в 28 дней
# (WEEKS_IN_WINDOW недель). Сравнивать их напрямую значит разрешить перерасход
# на 8,6 %: 28 дней подряд по потолку — это 30,44/28 потолка за месяц.
DAYS_IN_MONTH = 30.44
WINDOW_DAYS = WEEKS_IN_WINDOW * 7.0

# Допуск сверки «Σ целевых = бюджету», рубль. Бинарный поиск сходится с
# точностью до копеек, и объявлять невязкой их значило бы печатать шум.
GROWTH_RESIDUAL_RUB = 1.0


def account_budget(current_cost: float, lam: float, target_romi: float,
                   room_rub: float,
                   monthly_cap: Optional[float]) -> Dict[str, Any]:
    """Бюджет кабинета на такт: держим или растим, и чем ограничены.

    Три условия роста, и каждое закрывает свой способ сжечь деньги:

      * λ выше требуемой окупаемости С ЗАПАСОМ — иначе прибавка уходит за
        порог сразу, ещё до того как кривая насыщения её отработает;
      * есть куда потратить СЕГОДНЯ (room_rub — запас кампаний, у которых
        лимит связывает расход, growth.room_rub_budget). Запас, доступный
        только через эскалацию цены, сюда не подаётся: эти деньги кабинет
        физически не выберет, и они каждый такт изображали бы резерв;
      * потолок месячного освоения задан человеком. Не задан — предложение
        считается и печатается, но сумма не меняется: решение «тратить
        больше» принимает владелец денег, агент приносит ему цифру.

    capped_by называет ограничитель: "lambda" | "room" | "step" |
    "monthly_cap".
    """
    def hold(reason: str, proposed: float = 0.0) -> Dict[str, Any]:
        return {"budget": round(current_cost, 2), "growth_rub": 0.0,
                "proposed_growth_rub": round(proposed, 2), "capped_by": reason}

    # Безубыточность предельного рубля (lambda_breakeven) и запас над целью —
    # два разных требования. Второе при цели от 1.0 строже первого, но
    # условие роста должно читаться целиком, а не опираться на текущий
    # минимум панели настроек.
    if lam < 1.0 or lam < float(target_romi) * GROWTH_LAMBDA_MARGIN:
        return hold("lambda")
    if room_rub <= 0:
        return hold("room")

    step_rub = current_cost * ACCOUNT_GROWTH_STEP
    growth = min(step_rub, float(room_rub))
    capped_by = "step" if step_rub <= room_rub else "room"
    if monthly_cap is None:
        return hold(capped_by, proposed=growth)

    # Потолок ниже факта — это команда сокращать общую сумму, а сокращения по
    # кабинету агент не делает: перенос внутри кабинета решает солвер, а
    # объём освоения — владелец. Сумма остаётся, упор в потолок виден.
    cap_window = float(monthly_cap) * WINDOW_DAYS / DAYS_IN_MONTH
    budget = max(current_cost, min(current_cost + growth, cap_window))
    if budget < current_cost + growth - 1e-6:
        capped_by = "monthly_cap"
    return {"budget": round(budget, 2),
            "growth_rub": round(budget - current_cost, 2),
            "proposed_growth_rub": round(growth, 2),
            "capped_by": capped_by}


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


def binding_limit(settings: Optional[Dict[str, Any]], cost_28d: float) -> bool:
    """Связывает ли текущий лимит расход кампании — по витрине настроек.

    Повторяет правило рычага записи (writer/budget.py::diff_budget): повышение
    бюджета лимитом достижимо только там, где кампания в него упирается, —
    таких по замеру 9 из 62 (docs/AGENT-AUDIT-2026-08-23.md:214). Все отказы
    писателя тоже воспроизведены: пакетная стратегия, не TEXT_CAMPAIGN, лимит
    сразу в двух каналах и полное отсутствие лимита означают, что деньги в эту
    кампанию рычагом не доедут, — а значит и «связывает» тут неприменимо.

    В одном месте правило СТРОЖЕ писательского намеренно: тот пропускает
    кампанию с неизвестным типом (writer/budget.py:358 — `not in (None,
    "TEXT_CAMPAIGN")`), потому что отказать реальному действию по незнанию
    дороже, чем попробовать. Здесь наоборот: незнание не должно поднимать вес
    в разведке, иначе карман уйдёт кампании, про которую нечего сказать.

    Единицы: cost_28d — расход за 28 дней С НДС (все факты EDU), лимиты
    витрины — рубли БЕЗ НДС (edu_direct_settings переводит микрорубли).
    Конверсия та же, что у писателя: cost / 4 недель / VAT.
    """
    strategy = ((settings or {}).get("strategy") or {})
    if not isinstance(strategy, dict) or strategy.get("package"):
        return False
    if ((settings or {}).get("meta") or {}).get("campaignType") != "TEXT_CAMPAIGN":
        return False

    weekly_spend = float(cost_28d or 0.0) / WEEKS_IN_WINDOW / VAT
    if weekly_spend <= 0:
        return False

    limits = []
    for channel in ("search", "network"):
        block = strategy.get(channel)
        weekly = (block or {}).get("weeklyBudget") if isinstance(block, dict) else None
        if weekly:
            limits.append(float(weekly))
    if len(limits) > 1:
        return False
    if limits:
        return weekly_spend >= BINDING_SHARE * limits[0]

    daily = strategy.get("dailyBudget")
    if daily:
        return weekly_spend / 7.0 >= BINDING_SHARE * float(daily)
    return False


def step_cap_up(campaign: Dict[str, Any]) -> float:
    """Потолок шага вверх для кампании: ×1.5 обычно, ×2 при доказанном недоборе.

    Четыре условия сразу, и каждое отсекает свой способ сжечь деньги:

      * growth_room is True — недобор ИЗМЕРЕН. None («не мерили») права на ×2
        не даёт: это не то же самое, что измеренное «места нет»;
      * β < 1 — кривая не спорит с ростом;
      * предельный рубль возвращает BIG_STEP_ROI_MARGIN порога кабинета —
        запас на то, что вторая половина шага окупится хуже первой;
      * лимит связывает расход — иначе поднятый потолок не купит ни одного
        показа (замер рычага: 9 кампаний из 62), и писатель откажет
        NOT_APPLICABLE_UP_REASON, а солвер уже считал бы эти деньги
        распределёнными.
    """
    if (campaign.get("growth_room") is True
            and campaign.get("limit_binding") is True
            and float(campaign.get("beta") or 1.0) < 1.0
            and float(campaign.get("marginal_roi_vs_lambda") or 0.0)
            >= BIG_STEP_ROI_MARGIN):
        return BIG_STEP_UP
    return MAX_STEP_UP


def write_step_for(campaign: Dict[str, Any]) -> float:
    """Кап ЗАПИСИ для кампании: доля расхода, на которую писателю можно двигать.

    Обычные ±MAX_WRITE_STEP — граница, за которой стратегия переобучается.
    Расширенный кап солвера ровно её и переходит осознанно, и без этой
    передачи шаг ×2 умирал бы на последнем метре: писатель зажал бы цель
    до +20 %, а тесты остались бы зелёными — до него они не доходят.
    """
    cap = step_cap_up(campaign)
    return cap - 1.0 if cap > MAX_STEP_UP else MAX_WRITE_STEP


def _target_spend(campaign: Dict[str, Any], lam: float) -> float:
    """Целевой бюджет кампании при пороге λ, с капом шага.

    Считается в лог-пространстве: при β → 1 показатель 1/(1−β) взрывается, и
    прямое возведение в степень переполняется задолго до капа. β ≥ 1
    (насыщения нет) — порог внутри капа не пересекается; шаг в сторону
    текущей предельной окупаемости относительно λ, но умеренный
    (BETA_SUPERLINEAR_STEP), а не сразу в кап: см. комментарий у константы.
    """
    cost = campaign["cost"]
    roi_ratio = campaign["value"] / (lam * campaign["marginal_cpl"])
    log_ratio = math.log(roi_ratio)
    beta = campaign["beta"]
    if beta >= 1.0:
        return cost * (BETA_SUPERLINEAR_STEP if log_ratio > 0
                       else 1.0 / BETA_SUPERLINEAR_STEP)
    # Кап вверх — решение по кампании, а не общая константа: отношение к λ
    # известно только здесь, и передаётся готовым числом, чтобы step_cap_up
    # не пересчитывал порог второй раз.
    cap_up = step_cap_up({**campaign, "marginal_roi_vs_lambda": roi_ratio})
    scaled = log_ratio / (1.0 - beta)
    if scaled >= math.log(cap_up):
        return cost * cap_up
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
        # Недобор трафика поднимает ценность разведки: там, где ставка режет
        # объём, доливка отвечает на вопрос «сколько ещё есть», а на
        # выкупленной кампании тот же рубль отвечает только «дороже ли
        # следующий показ». Множитель 1..2 — линейный по недобору, без
        # свободных параметров.
        # Но только там, где лимит СВЯЗЫВАЕТ расход: надбавка — это деньги, а
        # деньги тратятся лишь у кампании, упирающейся в лимит (9 из 62).
        # Иначе прибавка зафиксировалась бы в отчёте и не превратилась ни в
        # показы, ни в знание; недобор такой кампании — повод для эскалации
        # цены, а не для разведочного рубля.
        room = (min(max(float(campaign.get("headroom_share") or 0.0), 0.0), 1.0)
                if campaign.get("limit_binding") else 0.0)
        weight = rel * float(campaign.get("cost") or 0.0) * (1.0 + room)
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


def _step_capped(campaign: Dict[str, Any], roi_ratio: float, cap_up: float) -> bool:
    """Хотел ли солвер дать больше, чем разрешает кап шага за такт.

    Считается по НЕЗАЖАТОМУ оптимуму (_target_spend до капа), а не сравнением
    итоговой цели с потолком: карман разведки изымает долю у всех, и кампания,
    упёршаяся в кап, приходит в отчёт чуть ниже него — сравнение с целью
    объявляло бы «в кап не упёрлись» ровно там, где упёрлись.

    β ≥ 1 в кап не упирается по построению: такой кампании положен отдельный
    умеренный шаг (BETA_SUPERLINEAR_STEP), и он ниже любого потолка.
    """
    beta = float(campaign.get("beta") or 0.0)
    if beta >= 1.0 or roi_ratio <= 0:
        return False
    return math.log(roi_ratio) / (1.0 - beta) >= math.log(cap_up)


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
    # Кап записи едет вместе со сдвигом и только вверх: расширенный шаг —
    # это про рост, и симметрично разжимать им кап вниз значило бы разрешить
    # обвал расхода за один такт. Обычные ±20 % писатель ставит сам, и
    # дублировать их строкой незачем.
    write_step = write_step_for({**campaign, "marginal_roi_vs_lambda": roi_ratio})
    # Упор в потолок шага: солвер хотел дать больше, чем разрешено за такт.
    # Это кандидат на усиление (agent/growth.py), а не законченное решение.
    capped = _step_capped(
        campaign, roi_ratio,
        step_cap_up({**campaign, "marginal_roi_vs_lambda": roi_ratio}))
    return {
        **({"switch_off": switch_off} if switch_off else {}),
        **({"write_step": write_step}
           if write_step > MAX_WRITE_STEP and ratio > 1.0 else {}),
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
        "step_capped": capped,
        # Рычаг роста этой кампании: доливка бюджета доедет только там, где
        # лимит уже связывает расход (9 из 62), остальным нужна цена.
        "limit_binding": bool(campaign.get("limit_binding")),
        "expected_leads_delta": round(delta_leads, 1),
        "expected_revenue_delta": round(delta_leads * campaign["value"], 2),
        "rel_error": round(rel, 4),
        "p_sign": verdict["p_sign"],
        "confident": verdict["confident"] is True,
    }


def _apply_exploration(
    targets: Dict[str, float], cost_by_id: Dict[str, float],
    explore_rub: float, campaigns: List[Dict[str, Any]], lam: float,
) -> Tuple[Dict[str, float], Dict[str, float]]:
    """Цели с изъятым и перераспределённым карманом разведки.

    Карман берётся ТОЛЬКО из запаса цели до нижнего капа шага: кампания,
    уже стоящая на полу (×MAX_STEP_DOWN), не должна проседать глубже — кап
    шага держится и при разведке. Столько же и раздаётся, поэтому сумма
    целевых по кабинету не меняется. Раздача ограничена верхним капом по той
    же причине; невыданный остаток возвращается источникам пропорционально
    изъятому, а не растворяется.

    Потолок раздачи — АДРЕСНЫЙ кап кампании (step_cap_up), а не общий ×1.5.
    Иначе кампания с доказанным недобором отдавала бы в карман свою долю
    наравне со всеми, а вернуть не могла: её цель уже стоит выше общего
    потолка, room выходит нулём, и заявленный ×2 садится примерно на ×1.8 —
    молча, потому что сумма кабинета при этом сходится.
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

    cap_by_id = {
        str(c["campaign_id"]): step_cap_up({
            **c,
            "marginal_roi_vs_lambda": (c["value"] / (lam * c["marginal_cpl"])
                                       if lam > 0 and c["marginal_cpl"] > 0 else 0.0),
        })
        for c in campaigns
    }
    ceilings = {cid: cost_by_id.get(cid, 0.0) * cap_by_id.get(cid, MAX_STEP_UP)
                for cid in targets}
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
    settings_by_campaign: Optional[Dict[str, Dict[str, Any]]] = None,
    target_romi: float = DEFAULT_TARGET_ROMI,
    room_rub_by_login: Optional[Dict[str, float]] = None,
    monthly_cap_rub: Optional[float] = None,
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

    room_rub_by_login — рубли, которые кабинет освоит СЕГОДНЯ поднятием
    лимитов (growth.room_rub_budget по кабинетам). Запас чужого кабинета в
    рост не складывается: счёт один на кабинет, и деньги внутри него.
    """
    holdout_ids = {str(h) for h in (holdout_ids or set())}
    # Настроек нет — «связывает ли лимит» неизвестно, и множитель недобора не
    # применяется никому: карман делится по одному незнанию, как до задачи 4.
    settings_by_campaign = settings_by_campaign or {}
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
            # Недобор трафика кривой (Э7.6): 0.0 — либо выкуплено, либо
            # величина не измерена. Различие между ними держит сама кривая
            # (growth_room = None), а карман разведки одинаково не поднимает
            # вес в обоих случаях: надбавка по недобору требует замера.
            "headroom_share": (float(curve["headroom_share"])
                               if curve.get("headroom_share") is not None else 0.0),
            "limit_binding": binding_limit(
                settings_by_campaign.get(str(campaign_id)),
                float(curve["cost_28d"])),
            # Трёхзначен: True — недобор измерен, False — измерен и места
            # нет, None — не мерили. Право на расширенный шаг даёт только
            # True (step_cap_up), и подменять None на False здесь нельзя.
            "growth_room": curve.get("growth_room"),
        })

    accounts: Dict[str, Dict[str, Any]] = {}
    room_rub_by_login = room_rub_by_login or {}
    for login, campaigns in sorted(by_login.items()):
        fact_cost = sum(c["cost"] for c in campaigns)
        if fact_cost <= 0:
            continue
        # Порог и вердикты считаются на ПОЛНОМ бюджете: карман не должен
        # двигать λ, иначе кампания у пола капа становится кандидатом на
        # выключение только потому, что часть денег ушла на разведку.
        lam, targets = solve_threshold(campaigns, fact_cost)
        # Растить или держать общую сумму — решается по порогу ПРИ ТЕКУЩЕМ
        # расходе: λ выросшего бюджета уже учитывает прибавку и сам по себе
        # её не оправдывает.
        growth_plan = account_budget(fact_cost, lam, target_romi,
                                     float(room_rub_by_login.get(login) or 0.0),
                                     monthly_cap_rub)
        budget = growth_plan["budget"]
        if budget > fact_cost:
            grown_lam, targets = solve_threshold(campaigns, budget)
            # Порог берётся из новой раскладки, только если она УПИРАЕТСЯ в
            # бюджет. Не упирается — капы шага не дают освоить прибавку,
            # ограничение «сумма = бюджету» на этом плато порог не задаёт, и
            # бинарный поиск уходит к нулю. Нулевой λ объявил бы кабинет
            # убыточным и раздул бы уверенность каждого сдвига — механизм
            # начал бы резать по мусорному числу.
            if sum(targets.values()) >= budget - GROWTH_RESIDUAL_RUB:
                lam = grown_lam
        preliminary = {c["campaign_id"]: _move_row(c, targets[c["campaign_id"]], lam)
                       for c in campaigns}
        # Карман изымается ПОСЛЕ решения — пропорционально у всех — и уходит
        # туда, где оценка хуже всего. Сумма целевых при этом не меняется:
        # (1−share)·Σцелей + share·бюджет = бюджет.
        cost_by_id = {c["campaign_id"]: c["cost"] for c in campaigns}
        targets, bonus = _apply_exploration(
            targets, cost_by_id, budget * EXPLORATION_SHARE,
            [{**c, "switch_off": bool(preliminary[c["campaign_id"]].get("switch_off"))}
             for c in campaigns], lam)
        moves = {c["campaign_id"]: _move_row(c, targets[c["campaign_id"]], lam)
                 for c in campaigns}
        target_sum = sum(targets.values())
        # Сверка суммы — на ИТОГОВЫХ целях, тех самых, что уедут писателю:
        # карман разведки изымает свою долю уже после раскладки, и проверять
        # числа до него значило бы проверять не то, что применяется.
        # Прибавка, которую капы шага не дают разложить, — не деньги в
        # работе, а невязка. Оставить её в бюджете значит каждый такт
        # дораспределять призрак: солвер видел бы недоданные рубли и задирал
        # цели тем, кто и так упёрся в кап. Признаём назначенное, а остаток
        # уедет на следующий такт, когда капы отсчитаются от нового расхода.
        deferred = 0.0
        if target_sum < budget - GROWTH_RESIDUAL_RUB:
            deferred = round(budget - target_sum, 2)
            budget = target_sum
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
            # Факт окна и прибавка к нему — двумя числами: «бюджет кабинета»
            # больше не равен расходу, и без обеих сторон непонятно, откуда
            # взялась разница.
            "cost_28d": round(fact_cost, 2),
            "growth_rub": round(budget - fact_cost, 2),
            # Сколько дал бы рост, будь потолок месяца задан. Пока ключа
            # monthly_budget_cap_rub нет, это единственное место, где видно
            # цену решения «тратить больше».
            "proposed_growth_rub": growth_plan["proposed_growth_rub"],
            "growth_capped_by": growth_plan["capped_by"],
            # Рубли прибавки, которые капы шага не дали разложить: они не
            # размазаны по кампаниям и не висят в бюджете, а перенесены на
            # следующий такт.
            "deferred_growth_rub": deferred,
            # Сколько денег кабинета ушло на разведку и кому: без этой строки
            # надбавка неотличима от решения солвера.
            "exploration": {
                "share": explore_share if bonus else 0.0,
                "rub": round(sum(bonus.values()), 2),
                "campaigns": len(bonus),
                # Кому недобор трафика поднял вес: замер есть, лимит
                # связывает. Ноль при живом недоборе — не поломка, а правда
                # о рычаге: доливать некуда, кампаниям нужна цена (Э4).
                "headroom_boosted": sum(
                    1 for c in campaigns
                    if c.get("limit_binding") and c.get("headroom_share")
                    and not preliminary[c["campaign_id"]].get("switch_off")),
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
                # Ожидаемый прирост лидов вдоль кривой — то самое число,
                # против которого сторож через 7–14 дней положит факт. Едет
                # готовым: пересчитывать формулу leads·((S*/S₀)^β − 1) в
                # писателе значило бы держать вторую копию модели, и первая
                # же правка кривой развела бы их молча. raw_value — та же
                # дельта в деньгах.
                "setting_kind": "budget_target",
                "setting_key": "expected_leads_delta",
                "value": m["expected_leads_delta"],
                "raw_value": m["expected_revenue_delta"],
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
            if m.get("write_step"):
                # Кап записи для ЭТОЙ кампании. Без строки писатель зажал бы
                # цель общими ±20 % от расхода, и адресный шаг ×2 не доехал
                # бы до кабинета ни разу. raw_value — сам сдвиг, чтобы в
                # журнале было видно, к чему кап приложен.
                rows.append({
                    "setting_kind": "budget_target",
                    "setting_key": "write_step",
                    "value": m["write_step"],
                    "raw_value": m["ratio"],
                    "support_n": m["leads_28d"],
                    "rel_error": m["rel_error"],
                })
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
