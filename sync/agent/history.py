# -*- coding: utf-8 -*-
"""
sync/agent/history.py — Э2.4: чтение истории. Квазиэксперименты как вход решений.

Каждый квазиэксперимент (mining.py) — уже оплаченный ответ на вопрос «что
случилось с ценой лида, когда бюджет этой кампании скакнул». Здесь эти ответы
сводятся в сигнал НАСЫЩЕНИЯ: эластичность цены лида по бюджету.

    eps = эффект на CPL (DiD, чистый от сезона) / ln(бюджет после / до)

  * eps > 0 — цена лида растёт с бюджетом: кампания насыщается, предельный
    лид дороже среднего. Верно в обе стороны скачка: при срезании бюджета
    CPL падает → effect < 0, ln(jump) < 0 → eps снова > 0.
  * eps < 0 — залили больше, а лид подешевел: насыщения не видно, есть запас.

Свод — обратновзвешенное по дисперсии среднее: сначала эксперименты одной
кампании, затем кампании направления. Уверенность — той же шкалой Э2.3
(p_sign против порога класса budget_shift): двигать бюджеты — среднеобратимое
действие, порог 0.90.

Класс надёжности B удержан смыслом, а не только меткой: DiD чистит сезон, но
не чистит причину самого скачка (трогали то, что уже болело). Поэтому выход
модуля — приоритет гипотез и вход кривых насыщения Э3.1, а не готовый приказ
«перелить бюджет».
"""

import math
from typing import Any, Dict, List, Optional

from sync.agent.confidence import assess

# Скачок меньше этого по модулю в логарифме не делит бюджет на «до» и «после»
# осмысленно: знаменатель эластичности стремится к нулю и раздувает eps из
# шума. Порог согласован с min_jump=0.3 детектора (ln 1.3 ≈ 0.26).
MIN_LOG_JUMP = 0.25


def elasticity(experiment: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """Эластичность цены лида по бюджету из одного эксперимента, или None.

    None — эксперимент непригоден для эластичности: нет эффекта, нет уровней
    бюджета, скачок слишком мал или ошибка не записана (строки старого
    формата с фиктивным интервалом не пересчитываются задним числом).
    """
    effect = experiment.get("effect")
    # Класс C — RTM-подозрительная предыстория (mining.pre_trend_check):
    # правку сделали В ОТВЕТ на всплеск, и возвращение всплеска к среднему
    # DiD припишет заслуге правки. Смещение систематическое, а не шумовое —
    # расширять интервал бесполезно, такое наблюдение в эластичность не идёт.
    if str(experiment.get("reliability_class") or "B") not in ("A", "B"):
        return None
    params = experiment.get("params") or {}
    before = float(params.get("before") or 0.0)
    after = float(params.get("after") or 0.0)
    rel = params.get("rel_error")
    if effect is None or before <= 0 or after <= 0 or not rel:
        return None
    log_jump = math.log(after / before)
    if abs(log_jump) < MIN_LOG_JUMP:
        return None
    return {
        "eps": float(effect) / log_jump,
        "rel_error": float(rel) / abs(log_jump),
    }


def combine(observations: List[Dict[str, float]]) -> Optional[Dict[str, float]]:
    """Обратновзвешенный свод наблюдений {eps, rel_error} с поправкой на разброс.

    Веса 1/σ² — точное наблюдение весит больше. Но собственная ошибка
    наблюдения пуассоновская, то есть НИЖНЯЯ граница неопределённости: недели
    различаются сезоном, конкурентами, качеством трафика, а соседние пары
    недель ещё и делят общую неделю — наблюдения не независимы. Чистый свод с
    фиксированным эффектом отчитывался о точности, которой нет: чем больше
    шумных наблюдений, тем уже интервал, сколько бы они ни спорили между
    собой (аудит 2026-08-23, C3).

    Поправка — множитель разброса S = √(Q/df) при Q > df (та же поправка, что
    у PDG для несовместимых измерений): наблюдения согласны — S = 1 и свод в
    точности прежний; спорят — интервал расширяется ровно во столько раз, во
    сколько разброс превышает заявленные ошибки. Веса при этом НЕ трогаются,
    в отличие от случайных эффектов DerSimonian–Laird: на двух наблюдениях
    (типичный случай кампании — один квазиэксперимент плюс одна пара недель)
    τ² оценивается по одной степени свободы и настолько шумит, что топит
    точное наблюдение в шумном. Расширить интервал честнее, чем переспорить
    точный замер.

    Одно наблюдение: разброса нет, S = 1, свод равен самому наблюдению.
    """
    weighted = [(o["eps"], 1.0 / (o["rel_error"] ** 2))
                for o in observations if o and o.get("rel_error")]
    if not weighted:
        return None
    total_w = sum(w for _, w in weighted)
    mean_eps = sum(v * w for v, w in weighted) / total_w

    scale = 1.0
    if len(weighted) > 1:
        q = sum(w * (v - mean_eps) ** 2 for v, w in weighted)
        df = len(weighted) - 1
        if q > df:
            scale = math.sqrt(q / df)

    return {
        "eps": mean_eps,
        "rel_error": math.sqrt(1.0 / total_w) * scale,
        "n": len(weighted),
        # Множитель разброса наружу: «наблюдений много, но они спорят» обязано
        # быть видно в отчёте, а не только в ширине интервала.
        "scale": round(scale, 3),
    }


def _judge(pooled: Dict[str, float]) -> Dict[str, Any]:
    """Вердикт по сведённой эластичности через шкалу Э2.3.

    eps живёт в лог-шкале отношения (относительный эффект на относительный
    скачок), поэтому в p_sign он передаётся как exp(eps) — «во сколько раз
    меняется CPL на e-кратный рост бюджета».
    """
    verdict = assess(math.exp(pooled["eps"]), pooled["rel_error"], "budget_shift")
    if verdict["confident"] is True:
        label = "насыщается" if pooled["eps"] > 0 else "не насыщена"
    else:
        label = "неопределённо"
    return {
        "eps": round(pooled["eps"], 4),
        "rel_error": round(pooled["rel_error"], 4),
        "experiments": pooled["n"],
        # Во сколько раз интервал расширен из-за спора наблюдений (combine).
        "scale": pooled.get("scale", 1.0),
        "p_sign": verdict["p_sign"],
        "verdict": label,
    }


def budget_response(
    experiments: List[Dict[str, Any]],
    direction_by_campaign: Dict[str, Optional[str]],
) -> Dict[str, Any]:
    """Сигнал насыщения по кампаниям и направлениям из квазиэкспериментов.

    Направление берётся из витрины фактов, а не из эксперимента: эксперимент
    привязан к кампании, и направление у неё одно и то же в обоих источниках.
    Кампании без направления сводятся под ключом "unknown" — молчать о них
    нельзя, это те же битые UTM, что и у пустышек лестницы.
    """
    usable: Dict[str, List[Dict[str, float]]] = {}
    skipped = 0
    for exp in experiments:
        obs = elasticity(exp)
        if obs is None:
            skipped += 1
            continue
        usable.setdefault(str(exp.get("object_id")), []).append(obs)

    campaigns: Dict[str, Dict[str, Any]] = {}
    by_direction: Dict[str, List[Dict[str, float]]] = {}
    for campaign_id, observations in sorted(usable.items()):
        pooled = combine(observations)
        if pooled is None:
            continue
        campaigns[campaign_id] = _judge(pooled)
        direction = direction_by_campaign.get(campaign_id) or "unknown"
        by_direction.setdefault(str(direction), []).extend(observations)

    directions: Dict[str, Dict[str, Any]] = {}
    for direction, observations in sorted(by_direction.items()):
        pooled = combine(observations)
        if pooled is not None:
            directions[direction] = _judge(pooled)
    confident = [c for c, row in campaigns.items() if row["verdict"] != "неопределённо"]
    return {
        "experiments_total": len(experiments),
        "experiments_unusable": skipped,
        "campaigns_with_signal": len(campaigns),
        "campaigns_confident": len(confident),
        "campaigns": campaigns,
        "directions": directions,
    }
