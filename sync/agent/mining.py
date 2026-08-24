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
from statistics import mean, pstdev
from typing import Any, Dict, List, Optional, Set

MIN_SIDE_DAYS = 7          # минимум дней с каждой стороны точки изменения

# Порог отклонения предыстории: во сколько СИГМ цена лида окна «до» отличается
# от собственной базы кампании ДО него. Больше порога — правку, скорее всего,
# сделали В ОТВЕТ на всплеск, а всплеск и сам возвращается к среднему
# (regression to the mean): DiD припишет это возвращение заслуге правки.
# Такое наблюдение не выбрасывается (сам факт изменения полезен), а
# ДЕКЛАССИРУЕТСЯ: класс C в эластичность не идёт (history.elasticity).
RTM_SIGMA_THRESHOLD = 1.0

# Класс надёжности RTM-подозрительного наблюдения. Ниже B: «трогали то, что
# болело» — общее смещение всех квазиэкспериментов; здесь оно ещё и измеримо.
RTM_CLASS = "C"

# Плацебо-DiD: минимум точек «где ничего не происходило», ниже которого
# разброс не оценивается — на двух-трёх наблюдениях он сам шум.
MIN_PLACEBO_POINTS = 5

# Шаг между плацебо-точками в днях. Соседние точки делят почти все свои дни,
# и их эффекты не были бы независимыми наблюдениями шума.
PLACEBO_STEP_DAYS = 7


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


def pre_trend_check(
    rows: List[Dict[str, Any]], before_dates: Set[str], window: int,
) -> Dict[str, Any]:
    """Отклонение цены лида окна «до» от собственной базы кампании перед ним.

    База — те же window суток, что лежат ДО окна «до». Сравнение в логарифмах
    и в сигмах пуассоновского счёта лидов обоих окон: z = |ln(cpl_до /
    cpl_база)| / √(1/лиды_до + 1/лиды_база).

    Базы нет (истории не хватает) или в окне нет лидов — судить не о чем:
    rtm_suspect=False с указанной причиной. Молчаливое «подозрительно» на
    нехватке данных деклассировало бы всю раннюю историю кабинета.
    """
    if not before_dates:
        return {"rtm_suspect": False, "reason": "окно «до» пусто"}
    baseline_days = sorted({r["fact_date"] for r in rows
                            if r["fact_date"] < min(before_dates)})[-window:]
    if len(baseline_days) < window:
        return {"rtm_suspect": False, "reason": "истории до окна не хватает"}
    pre = _window_metrics([r for r in rows if r["fact_date"] in before_dates])
    base = _window_metrics([r for r in rows if r["fact_date"] in set(baseline_days)])
    if pre["leads"] <= 0 or base["leads"] <= 0 or pre["cpl"] <= 0 or base["cpl"] <= 0:
        return {"rtm_suspect": False, "reason": "в окне или базе нет лидов"}
    sigma = math.sqrt(1.0 / pre["leads"] + 1.0 / base["leads"])
    z = abs(math.log(pre["cpl"] / base["cpl"])) / sigma if sigma > 0 else 0.0
    return {
        "rtm_suspect": z > RTM_SIGMA_THRESHOLD,
        "z": round(z, 2),
        "cpl_before_window": round(pre["cpl"], 2),
        "cpl_baseline": round(base["cpl"], 2),
    }


def _did_at(rows: List[Dict[str, Any]], control_rows: List[Dict[str, Any]],
            change_date: str, window: int) -> Optional[float]:
    """DiD-эффект в названной точке, или None, если окна не полны."""
    before_dates = _window_dates(rows, change_date, window, before=True)
    after_dates = _window_dates(rows, change_date, window, before=False)
    if len(before_dates) < window or len(after_dates) < window:
        return None
    windows = {
        "tb": _window_metrics([r for r in rows if r["fact_date"] in before_dates]),
        "ta": _window_metrics([r for r in rows if r["fact_date"] in after_dates]),
        "cb": _window_metrics([r for r in control_rows
                               if r["fact_date"] in before_dates]),
        "ca": _window_metrics([r for r in control_rows
                               if r["fact_date"] in after_dates]),
    }
    if any(w["leads"] == 0 for w in windows.values()):
        return None
    measured = did_effect(windows["tb"]["cpl"], windows["ta"]["cpl"],
                          windows["cb"]["cpl"], windows["ca"]["cpl"], 0.0)
    return measured["effect"]


def placebo_sigma(facts: List[Dict[str, Any]], window: int = 14,
                  step: int = PLACEBO_STEP_DAYS) -> Optional[float]:
    """Разброс DiD-эффектов там, где НИЧЕГО не менялось.

    В точке без изменения истинный эффект равен нулю, и всё, что DiD там
    показывает, — шум: сезон, конкуренты, качество трафика, случайные
    колебания конверсии. Пуассоновская ошибка счётчиков этого не знает и
    систематически занижает неопределённость (аудит 2026-08-23: «DiD без
    placebo»).

    Поэтому разброс плацебо-эффектов становится ПОЛОМ ошибки любого
    измеренного эффекта: реальная оценка не может быть точнее, чем шум на
    пустом месте. Точек меньше MIN_PLACEBO_POINTS — оценка сама шум, и пола
    нет (None), а не выдуманный ноль.

    Точки берутся с шагом step и в стороне от найденных скачков: окно вокруг
    настоящего изменения — не пустое место.
    """
    if not facts:
        return None
    by_campaign: Dict[str, List[Dict[str, Any]]] = {}
    for f in facts:
        by_campaign.setdefault(str(f["campaign_id"]), []).append(f)

    effects: List[float] = []
    for campaign_id, rows in sorted(by_campaign.items()):
        control_rows = [f for f in facts if str(f["campaign_id"]) != campaign_id]
        series = [{"date": r["fact_date"], "value": float(r.get("cost") or 0.0)}
                  for r in rows]
        change_days = {p["date"] for p in detect_change_points(series)}
        days = sorted({r["fact_date"] for r in rows})
        for i in range(window, len(days) - window + 1, step):
            point = days[i]
            if any(abs((date.fromisoformat(point)
                        - date.fromisoformat(d)).days) <= window
                   for d in change_days):
                continue
            effect = _did_at(rows, control_rows, point, window)
            if effect is not None:
                effects.append(effect)
    if len(effects) < MIN_PLACEBO_POINTS:
        return None
    return pstdev(effects)


def _experiment_id(campaign_id: str, change_date: str) -> str:
    raw = f"quasi:{campaign_id}:{change_date}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def mine_quasi_experiments(facts: List[Dict[str, Any]], window: int = 14,
                           error_floor: Optional[float] = None,
                           ) -> List[Dict[str, Any]]:
    """Находит изменения бюджета в истории и меряет их эффект через DiD.

    error_floor — уже посчитанный плацебо-разброс (placebo_sigma). Проход по
    плацебо-точкам дорогой, а потребителей у него два (ещё пары недель в
    saturation), поэтому прогон Э0 считает его один раз и передаёт сюда.
    None — считаем сами: вызывающий код без пола не должен молча остаться
    с пуассоновской недооценкой.
    """
    by_campaign: Dict[str, List[Dict[str, Any]]] = {}
    for f in facts:
        by_campaign.setdefault(str(f["campaign_id"]), []).append(f)

    # Пол ошибки из плацебо-точек — один на прогон: шум «на пустом месте»
    # общий для кабинета, и мерить его по каждой кампании отдельно значило бы
    # оценивать разброс по трём точкам.
    floor = (float(error_floor) if error_floor is not None
             else (placebo_sigma(facts, window=window) or 0.0))

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

            # Пуассоновский счёт — нижняя граница; плацебо показывает, какой
            # разброс даёт DiD там, где эффекта нет вовсе. Берём большее.
            rel = max(did_rel_error(*(w["leads"] for w in windows.values())), floor)
            measured = did_effect(
                windows["treated_before"]["cpl"], windows["treated_after"]["cpl"],
                windows["control_before"]["cpl"], windows["control_after"]["cpl"],
                rel,
            )
            if measured["effect"] is None:
                continue

            pre_trend = pre_trend_check(rows, before_dates, window)
            out.append({
                "experiment_id": _experiment_id(campaign_id, change_date),
                "hypothesis_type": "budget_change",
                "object_level": "campaign",
                "object_id": campaign_id,
                "params": {
                    "jump": point["jump"], "before": point["before"], "after": point["after"],
                    "rel_error": round(rel, 4),
                    "placebo_sigma": round(floor, 4),
                    "leads": {k: w["leads"] for k, w in windows.items()},
                    "pre_trend": pre_trend,
                },
                "mechanism": "did",
                "started_on": change_date,
                "measured_on": change_date,
                "metric": "eff_cpl",
                "verdict": "improved" if measured["effect"] < 0 else "worsened",
                "reliability_class": RTM_CLASS if pre_trend["rtm_suspect"] else "B",
                "source": "quasi",
                **measured,
            })
    return out
