# -*- coding: utf-8 -*-
"""
sync/agent/demand.py — рыночный спрос как режим, а не как фон.

edu_wordstat_demand хранит недельную частоту по фразам. Без привязки к
направлениям кампаний это цифры ни о чём: агент видит падение лидов на СПО и
не знает, упал ли рынок целиком. Режим спроса меняет ОЖИДАНИЯ от кампании:
на сезонном спаде рост CPL — свойство рынка, а не провал кампании, и резать
её за это значит наказывать за погоду.

Режим определяется отклонением последней недели от медианы базового окна в
единицах разброса самого окна. Медиана, а не среднее: одна аномальная неделя
(праздники, сбой выгрузки) сдвигает среднее и делает следующую неделю
«подъёмом». Разброс — медианное абсолютное отклонение по той же причине.

Два ограничения, которые здесь названы, а не спрятаны:

1. **Ряд есть не у всех направлений.** EDU_DEMAND_PHRASES покрывает СПО, ВПО,
   дистант и ДПО. У 'school', 'it', 'med', 'mti', 'ntb', 'transfer' фраз нет
   вовсе, и 'school' среди них — самое свежее направление кабинета (запуск
   14.08.2026). Такие направления получают вердикт REGIME_NO_SERIES отдельно
   от REGIME_LOW_DATA: «нет ряда» лечится добавлением фраз в
   sync/edu_demand.py, «мало данных» — временем. Один вердикт на оба спрятал
   бы дыру в семантике спроса навсегда.

2. **Регион только 'ru'.** В витрине есть и 'msk', но гео кампании нигде не
   вычисляется (classify.normalize_city_ip_segment работает по городу ЛИДА,
   а не по настройкам кампании), сопоставить московский срез спроса с
   московскими кампаниями нечем. Всероссийский ряд включает Москву, поэтому
   режим по нему — консервативное приближение; московские строки в него не
   складываются, иначе москвичи считались бы дважды. Ограничение записано в
   docs/AGENT-DATA-SOURCES.md.
"""

import math
import statistics
from typing import Any, Dict, List

# Фраза → направление кампаний (sync/classify.py::detect_direction).
# 'dpo' в классификаторе кампаний отсутствует намеренно: под этот спрос
# кампаний нет, и видеть это отдельной строкой полезнее, чем растворить его
# в 'other'.
DIRECTION_BY_PHRASE: Dict[str, str] = {
    "колледж": "spo", "техникум": "spo", "училище": "spo", "ссуз": "spo",
    "среднее профессиональное": "spo",
    "вуз": "vpo", "университет": "vpo", "институт": "vpo",
    "высшее образование": "vpo", "бакалавриат": "vpo", "специалитет": "vpo",
    "магистратура": "vpo", "аспирантура": "vpo",
    "дистанционное обучение": "dist", "дистанционное образование": "dist",
    "заочное обучение": "dist",
    "переподготовка": "dpo", "повышение квалификации": "dpo",
    "профпереподготовка": "dpo",
}

# Направления detect_direction, под которые фраз спроса нет ни одной.
# Список из sync/classify.py::detect_direction минус те, что покрыты
# DIRECTION_BY_PHRASE; 'other' — не направление, а корзина остатка, и ждать
# от неё ряда спроса бессмысленно.
DIRECTIONS_WITHOUT_SERIES = ("school", "it", "med", "mti", "ntb", "transfer")

REGION = "ru"

REGIME_RISE = "подъём"
REGIME_FALL = "спад"
REGIME_NORMAL = "норма"
REGIME_LOW_DATA = "мало данных"
REGIME_NO_SERIES = "нет ряда"

# Базовое окно сравнения: восемь недель до последней. Короче — режим ловится
# на шуме, длиннее — сезонный сдвиг размазывается по собственной базе.
BASELINE_WEEKS = 8

# Минимум недель базы, при котором вердикт вообще выносится.
MIN_BASELINE_WEEKS = 6

# Порог режима в единицах разброса базы. 2 — обычная граница «это не шум»
# для симметричного отклонения.
REGIME_SIGMA = 2.0

# Приведение медианного абсолютного отклонения к σ нормального распределения.
# Множитель 1/Φ⁻¹(0.75) = 1.4826 — иначе порог «в двух сигмах» означал бы
# разное на разных рядах.
MAD_TO_SIGMA = 1.4826


def _spread(baseline: List[int], median: float) -> float:
    """Разброс базы с полом на уровне шума счёта.

    Частота Wordstat — счётная величина, и её собственный шум порядка √median
    (пуассоновский). MAD ниже этого пола означает не «ряд идеально ровный», а
    округление на стороне выгрузки: у ровной базы MAD = 0, и деление на него
    объявило бы удвоение спроса нормой (sigma = 0) — ровно наоборот смыслу.
    """
    mad = statistics.median([abs(v - median) for v in baseline]) * MAD_TO_SIGMA
    return max(mad, math.sqrt(max(median, 0.0)))


def weekly_demand_by_direction(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """{направление: {неделя: суммарная частота фраз направления}}."""
    out: Dict[str, Dict[str, int]] = {}
    for row in rows:
        if str(row.get("region") or REGION) != REGION:
            continue
        direction = DIRECTION_BY_PHRASE.get(str(row.get("phrase") or "").strip())
        if direction is None:
            continue
        week = str(row.get("week_start"))[:10]
        by_week = out.setdefault(direction, {})
        by_week[week] = by_week.get(week, 0) + int(row.get("frequency") or 0)
    return out


def demand_regime(rows: List[Dict[str, Any]], through_week: str,
                  baseline_weeks: int = BASELINE_WEEKS) -> Dict[str, Dict[str, Any]]:
    """Режим спроса по направлениям на неделю through_week."""
    out: Dict[str, Dict[str, Any]] = {}
    for direction, by_week in weekly_demand_by_direction(rows).items():
        weeks = sorted(w for w in by_week if w <= through_week)
        if not weeks:
            continue
        last_week = weeks[-1]
        frequency = by_week[last_week]
        baseline = [by_week[w] for w in weeks[:-1][-baseline_weeks:]]

        row: Dict[str, Any] = {
            "last_week": last_week,
            "frequency": frequency,
            "baseline_median": None,
            "deviation": None,
            "sigma": None,
            "regime": REGIME_LOW_DATA,
        }
        if len(baseline) >= MIN_BASELINE_WEEKS:
            median = statistics.median(baseline)
            spread = _spread(baseline, median)
            deviation = frequency - median
            sigma = (deviation / spread) if spread > 0 else 0.0
            row.update({
                "baseline_median": median,
                "deviation": deviation,
                "sigma": round(sigma, 2),
                "regime": (REGIME_RISE if sigma >= REGIME_SIGMA
                           else REGIME_FALL if sigma <= -REGIME_SIGMA
                           else REGIME_NORMAL),
            })
        out[direction] = row

    # Направления без единой фразы спроса: вердикт свой, не «мало данных».
    for direction in DIRECTIONS_WITHOUT_SERIES:
        out.setdefault(direction, {
            "last_week": None, "frequency": None, "baseline_median": None,
            "deviation": None, "sigma": None, "regime": REGIME_NO_SERIES,
        })
    return out


def directions_without_series(regimes: Dict[str, Dict[str, Any]]) -> List[str]:
    """Направления, у которых ряда спроса нет вовсе — отдельной строкой отчёта."""
    return sorted(d for d, row in regimes.items()
                  if row.get("regime") == REGIME_NO_SERIES)
