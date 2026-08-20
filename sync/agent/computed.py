# -*- coding: utf-8 -*-
"""
sync/agent/computed.py — вычисляемые настройки (корректировки и расписание).

Это НЕ гипотезы. Корректировка по сегменту — оценка отношения конверсионности
сегмента к базовой, сжатая к 1 пропорционально объёму данных (эмпирический Байес).
Мало наблюдений → корректировка близка к нулю; много → близка к наблюдаемому отношению.

Почему не A/B: при ~450 оплатах в год на аккаунт тест каждой корректировки сжёг бы
всю пропускную способность экспериментов на то, что считается формулой.

МЕТРИКА КОНВЕРСИОННОСТИ — конверсии среза, делённые на клики сегмента:
  · корректировки по сегментам — Conversions из сегментного отчёта Директа
    (достижения целей кабинета, то есть эффективные лиды);
  · расписание — достижения целей по часам из Метрики.

Так было не всегда, и в этом был фундаментальный дефект: ожидаемые оплаты
раздавались по сегментам ПРОПОРЦИОНАЛЬНО ДОЛЕ КЛИКОВ, после чего
конверсионность = оплаты/клики давала всем сегментам среза ОДНО И ТО ЖЕ число.
Корректировки различались только весом байесовского сжатия — то есть сегменты
не различали вовсе. Порог по оплатам на уровне кампании всё равно недостижим
(ни одна из 84 кампаний его не проходит), поэтому честные конверсии Директа
лучше красиво размазанных оплат.

База берётся ИЗ ТОГО ЖЕ СРЕЗА (Σ конверсий / Σ кликов), а не приходит снаружи:
внешняя база в других единицах (оплаты вместо конверсий) делает отношение
бессмысленным, и это ровно тот класс ошибки, который здесь уже случался.

Два вырождения дают отказ от корректировок с явной причиной, а не тихие нули:
  1. в срезе нет ни одной конверсии — считать нечего;
  2. у всех сегментов среза конверсионность совпала — данные не несут информации
     о сегментах (сигнатура дефекта выше).
"""

from math import isclose
from typing import Any, Dict, List, Optional, Tuple

PRIOR_WEIGHT = 50.0       # эквивалентное число наблюдений у априорного значения
MODIFIER_CAP = 0.5        # потолок корректировки ±50%
MIN_SUPPORT = 30          # ниже этого числа кликов сегмент не выводим вовсе
# Допуск сравнения конверсионностей. Разброс меньше него — это шум округления
# double, а не различие сегментов: размазывание по доле кликов давало именно его.
CONV_REL_TOL = 1e-9

NO_CLICKS_REASON = "в срезе нет кликов"
NO_CONVERSIONS_REASON = (
    "в срезе нет ни одной конверсии — конверсионность сегментов не считается; "
    "проверить, что цели переданы в запрос отчёта и настроены в кабинете"
)
LOW_SUPPORT_REASON = f"ни один сегмент среза не набрал {MIN_SUPPORT} кликов"
DEGENERATE_REASON = (
    "конверсионность всех сегментов среза одинакова — данные не различают сегменты, "
    "корректировки были бы вырожденными (сюда же попадает срез с единственным "
    "сегментом выше порога: сравнивать не с чем)"
)


def shrink_ratio(
    segment_conv: float, segment_n: int, base_conv: float, prior_weight: float = PRIOR_WEIGHT
) -> float:
    """Отношение конверсии сегмента к базовой, сжатое к 1 при малом объёме."""
    if segment_n <= 0 or base_conv <= 0:
        return 1.0
    weight = segment_n / (segment_n + prior_weight)
    shrunk = weight * segment_conv + (1.0 - weight) * base_conv
    return shrunk / base_conv


def bid_modifier_percent(ratio: float, cap: float = MODIFIER_CAP) -> int:
    """Отношение → процент корректировки ставки, ограниченный потолком."""
    raw = ratio - 1.0
    clipped = max(-cap, min(cap, raw))
    return int(round(clipped * 100))


def _conv_rate(row: Dict[str, Any], conv_key: str) -> float:
    clicks = int(row.get("clicks") or 0)
    return (float(row.get(conv_key) or 0.0) / clicks) if clicks > 0 else 0.0


def _all_equal(values: List[float]) -> bool:
    """Все конверсионности совпали (с точностью до шума double)."""
    first = values[0]
    return all(isclose(v, first, rel_tol=CONV_REL_TOL, abs_tol=0.0) for v in values)


def _emit(
    rows: List[Dict[str, Any]], kind_prefix: str, conv_key: str
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Срез → (строки настроек, причина отказа). Одно из двух всегда пусто."""
    total_clicks = sum(int(r.get("clicks") or 0) for r in rows)
    total_conv = sum(float(r.get(conv_key) or 0.0) for r in rows)
    if total_clicks <= 0:
        return [], NO_CLICKS_REASON
    if total_conv <= 0:
        return [], NO_CONVERSIONS_REASON

    base_conv = total_conv / total_clicks
    supported = [r for r in rows if int(r.get("clicks") or 0) >= MIN_SUPPORT]
    if not supported:
        return [], LOW_SUPPORT_REASON

    # Единственный сегмент сравнивать не с чем — _all_equal на списке из одного
    # элемента истинно, и это правильный ответ: отношение к базе, которую этот же
    # сегмент и задаёт, даёт ровно 1, то есть нулевую корректировку без информации.
    conv_rates = [_conv_rate(r, conv_key) for r in supported]
    if _all_equal(conv_rates):
        return [], DEGENERATE_REASON

    out: List[Dict[str, Any]] = []
    for row, conv in zip(supported, conv_rates):
        clicks = int(row["clicks"])
        ratio = shrink_ratio(conv, clicks, base_conv)
        out.append({
            "setting_kind": f"{kind_prefix}:{row['segment_kind']}",
            "setting_key": str(row["segment_key"]),
            "value": float(bid_modifier_percent(ratio)),
            "support_n": clicks,
            "raw_value": round(ratio, 4),
        })
    return out, None


def compute_segment_modifiers(
    rows: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Корректировки по сегментам: устройство, пол, возраст, гео.

    Конверсионность — Conversions сегментного отчёта Директа на клики сегмента.
    """
    return _emit(rows, "bid_modifier", "conversions")


def compute_schedule(
    rows: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Почасовой профиль — та же механика на достижениях целей из Метрики."""
    return _emit(rows, "schedule", "sum_p_pay")
