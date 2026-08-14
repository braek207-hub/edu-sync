# -*- coding: utf-8 -*-
"""
sync/agent/computed.py — вычисляемые настройки (корректировки и расписание).

Это НЕ гипотезы. Корректировка по сегменту — оценка отношения конверсионности
сегмента к базовой, сжатая к 1 пропорционально объёму данных (эмпирический Байес).
Мало наблюдений → корректировка близка к нулю; много → близка к наблюдаемому отношению.

Почему не A/B: при ~450 оплатах в год на аккаунт тест каждой корректировки сжёг бы
всю пропускную способность экспериментов на то, что считается формулой.

Метрика конверсии — Σ p_pay / клики (ожидаемые оплаты), не лиды: иначе побеждает
сегмент, дающий много дешёвых мусорных лидов.
"""

from typing import Any, Dict, List

PRIOR_WEIGHT = 50.0       # эквивалентное число наблюдений у априорного значения
MODIFIER_CAP = 0.5        # потолок корректировки ±50%
MIN_SUPPORT = 30          # ниже этого числа кликов сегмент не выводим вовсе


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


def _emit(rows: List[Dict[str, Any]], base_conv: float, kind_prefix: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in rows:
        clicks = int(r.get("clicks") or 0)
        if clicks < MIN_SUPPORT:
            continue
        conv = float(r.get("sum_p_pay") or 0.0) / clicks
        ratio = shrink_ratio(conv, clicks, base_conv)
        out.append({
            "setting_kind": f"{kind_prefix}:{r['segment_kind']}",
            "setting_key": str(r["segment_key"]),
            "value": float(bid_modifier_percent(ratio)),
            "support_n": clicks,
            "raw_value": round(ratio, 4),
        })
    return out


def compute_segment_modifiers(rows: List[Dict[str, Any]], base_conv: float) -> List[Dict[str, Any]]:
    """Корректировки по сегментам: устройство, пол, возраст, гео."""
    return _emit(rows, base_conv, "bid_modifier")


def compute_schedule(rows: List[Dict[str, Any]], base_conv: float) -> List[Dict[str, Any]]:
    """Почасовой профиль — та же механика, отдельный вид настройки."""
    return _emit(rows, base_conv, "schedule")
