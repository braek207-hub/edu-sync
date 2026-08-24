# -*- coding: utf-8 -*-
"""
sync/agent/mining.py — квазиэксперименты из истории кабинета.

За окно истории уже произошли сотни изменений: менялись бюджеты, ставки, стратегии.
Каждое — естественный эксперимент, который уже оплачен и уже дал результат.
Находим момент изменения по скачку управляемого параметра и меряем эффект через DiD
против остальных кампаний за тот же период — сезон вычитается сам.

Смещение осознанное: трогали обычно то, что уже плохо работало, а плохое само
склонно вернуться к среднему. Поэтому класс надёжности B (не A) и умышленно широкий
доверительный интервал: такие оценки задают приоритет гипотез, но не дают права
двигать большие деньги.
"""

import hashlib
import math
from datetime import date
from statistics import mean
from typing import Any, Dict, List, Optional, Set

MIN_SIDE_DAYS = 7          # минимум дней с каждой стороны точки изменения


def detect_change_points(series: List[Dict[str, Any]], min_jump: float = 0.3) -> List[Dict[str, Any]]:
    """Точки скачка среднего уровня ряда: |после − до| / до ≥ min_jump."""
    ordered = sorted(series, key=lambda r: r["date"])
    if len(ordered) < MIN_SIDE_DAYS * 2:
        return []
    points: List[Dict[str, Any]] = []
    for i in range(MIN_SIDE_DAYS, len(ordered) - MIN_SIDE_DAYS + 1):
        before = mean(float(r["value"]) for r in ordered[i - MIN_SIDE_DAYS:i])
        after = mean(float(r["value"]) for r in ordered[i:i + MIN_SIDE_DAYS])
        if before <= 0:
            continue
        jump = abs(after - before) / before
        if jump >= min_jump:
            points.append({
                "date": ordered[i]["date"],
                "before": round(before, 2),
                "after": round(after, 2),
                "jump": round(jump, 4),
            })
    # Схлопываем соседние срабатывания одного и того же скачка: скользящее окно
    # реагирует на каждый день вокруг ступени. Группируем по близости дат и берём
    # вершину — момент, где отношение «после/до» максимально, и есть дата изменения.
    deduped: List[Dict[str, Any]] = []
    group: List[Dict[str, Any]] = []

    def _flush() -> None:
        if group:
            deduped.append(max(group, key=lambda p: p["jump"]))
            group.clear()

    for p in points:
        if group:
            prev_day = date.fromisoformat(group[-1]["date"])
            curr_day = date.fromisoformat(p["date"])
            if (curr_day - prev_day).days > MIN_SIDE_DAYS:
                _flush()
        group.append(p)
    _flush()
    return deduped


def did_rel_error(*lead_counts: int) -> float:
    """Относительная ошибка DiD-оценки из счётчиков лидов четырёх окон.

    Каждое из четырёх окон (обработанная/контроль × до/после) вносит
    пуассоновскую дисперсию своего счётчика; дисперсии складываются, потому
    что окна независимы. У контроля лидов тысячи и его вклад мал — ошибку
    задают окна обработанной кампании.
    """
    return math.sqrt(sum(1.0 / max(int(n), 1) for n in lead_counts))


def did_effect(
    treated_before: float, treated_after: float,
    control_before: float, control_after: float,
    rel_error: float,
) -> Dict[str, Optional[float]]:
    """Difference-in-differences: изменение у обработанной минус изменение у контроля.

    Шкала ЛОГАРИФМИЧЕСКАЯ: ln(после/до) обработанной минус то же у контроля.
    Эффект уходит дальше в эластичность делением на ЛОГАРИФМ скачка бюджета
    (history.elasticity, saturation.weekly_pair_observations), и прежняя
    арифметическая разность долей делилась на логарифмическую величину —
    несовместимые шкалы раздували |eps| тем сильнее, чем крупнее скачок
    (аудит 2026-08-23). На малых изменениях обе шкалы совпадают, на
    двукратных расходятся в полтора раза.

    Интервал — из пуассоновской ошибки счётчиков лидов (did_rel_error), одна
    сигма. Прежний фиктивный интервал «доля от эффекта + запас» не имел
    источника и делал уверенные оценки неотличимыми от шумных.

    Нулевой уровень в любом из четырёх окон делает оценку невозможной:
    логарифма нуля нет, и «эффект −100 %» здесь был бы выдумкой.
    """
    if min(treated_before, treated_after, control_before, control_after) <= 0:
        return {"effect": None, "effect_lo": None, "effect_hi": None}
    effect = (math.log(treated_after / treated_before)
              - math.log(control_after / control_before))
    return {
        "effect": round(effect, 4),
        "effect_lo": round(effect - rel_error, 4),
        "effect_hi": round(effect + rel_error, 4),
    }


def _window_metrics(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    """Расход, эффективные лиды и цена лида окна.

    Валюта — eff_leads, как у красных линий сторожа и базового CPA: Э2.0
    закрыл дорогу sum_p_pay на агрегатном уровне (по направлениям хуже наивной
    оценки на 23 %), а квазиэксперименты читаются именно агрегатно.
    """
    cost = sum(float(r.get("cost") or 0.0) for r in rows)
    leads = sum(int(r.get("eff_leads") or 0) for r in rows)
    return {"cost": cost, "leads": leads,
            "cpl": cost / leads if leads > 0 else 0.0}


def _window_dates(
    rows: List[Dict[str, Any]], change_date: str, window: int, before: bool
) -> Set[str]:
    """window суток обработанной кампании по одну сторону от точки перелома.

    Уникальные даты, а не строки: у контроля на ту же дату приходится по строке
    на кампанию, и сравнивать надо календарные окна, а не длины списков.
    """
    side = [r["fact_date"] for r in rows
            if (r["fact_date"] < change_date) == before]
    days = sorted(set(side))
    return set(days[-window:] if before else days[:window])


def _experiment_id(campaign_id: str, change_date: str) -> str:
    raw = f"quasi:{campaign_id}:{change_date}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def mine_quasi_experiments(facts: List[Dict[str, Any]], window: int = 14) -> List[Dict[str, Any]]:
    """Находит изменения бюджета в истории и меряет их эффект через DiD."""
    by_campaign: Dict[str, List[Dict[str, Any]]] = {}
    for f in facts:
        by_campaign.setdefault(str(f["campaign_id"]), []).append(f)

    out: List[Dict[str, Any]] = []
    for campaign_id, rows in sorted(by_campaign.items()):
        control_rows = [f for f in facts if str(f["campaign_id"]) != campaign_id]
        series = [{"date": r["fact_date"], "value": float(r.get("cost") or 0.0)} for r in rows]
        for point in detect_change_points(series):
            change_date = point["date"]
            # Окно задают ДАТЫ, а не число строк. Обработанная кампания даёт одну
            # строку в день, контроль — по строке на каждую из десятков кампаний,
            # поэтому срез «последние window строк» брал у контроля примерно
            # window/N суток (при 84 кампаниях — шестую часть одного дня) и всегда
            # одни и те же кампании с наибольшими id. DiD сравнивал две недели
            # обработанной с четвертью суток чужой выборки.
            before_dates = _window_dates(rows, change_date, window, before=True)
            after_dates = _window_dates(rows, change_date, window, before=False)
            treated_before = [r for r in rows if r["fact_date"] in before_dates]
            treated_after = [r for r in rows if r["fact_date"] in after_dates]
            control_before = [r for r in control_rows if r["fact_date"] in before_dates]
            control_after = [r for r in control_rows if r["fact_date"] in after_dates]
            if not (treated_before and treated_after and control_before and control_after):
                continue

            windows = {
                "treated_before": _window_metrics(treated_before),
                "treated_after": _window_metrics(treated_after),
                "control_before": _window_metrics(control_before),
                "control_after": _window_metrics(control_after),
            }
            if any(w["leads"] == 0 for w in windows.values()):
                # Окно без единого лида не даёт конечной цены лида: ноль лидов
                # после скачка бюджета — сигнал сам по себе, но мерить его как
                # «CPL вырос на X %» нельзя. Такой скачок эксперимента не даёт.
                continue

            rel = did_rel_error(*(w["leads"] for w in windows.values()))
            measured = did_effect(
                windows["treated_before"]["cpl"], windows["treated_after"]["cpl"],
                windows["control_before"]["cpl"], windows["control_after"]["cpl"],
                rel,
            )
            if measured["effect"] is None:
                continue

            out.append({
                "experiment_id": _experiment_id(campaign_id, change_date),
                "hypothesis_type": "budget_change",
                "object_level": "campaign",
                "object_id": campaign_id,
                "params": {
                    "jump": point["jump"], "before": point["before"], "after": point["after"],
                    "rel_error": round(rel, 4),
                    "leads": {k: w["leads"] for k, w in windows.items()},
                },
                "mechanism": "did",
                "started_on": change_date,
                "measured_on": change_date,
                "metric": "eff_cpl",
                "verdict": "improved" if measured["effect"] < 0 else "worsened",
                "reliability_class": "B",
                "source": "quasi",
                **measured,
            })
    return out
