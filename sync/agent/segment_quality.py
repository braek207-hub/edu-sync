# -*- coding: utf-8 -*-
"""
sync/agent/segment_quality.py — Э2.2b: качество лида по сегменту, не только цена.

Сегментные корректировки считаются по конверсии клик→лид из отчёта Директа.
Дефект рамки (вопрос Павла): сегмент может давать МНОГО лидов, которые ХУЖЕ
доходят до денег. Замер по мосту client_id × Метрика (прогон 32622086445)
показал ровно это: планшетный лид приносит 792 ₽ против 1625 ₽ у ПК, и
соединение 34,5 % против 49,7 % — при том, что по «конверсии в лид» планшеты
могут выглядеть лучше и получать плюс.

Поправка: корректировка сегмента = (конверсия в лид) × (качество лида сегмента).
Качество = ожидаемая выручка на лид сегмента к базе моста, посчитанная ЛЕСТНИЦЕЙ
(Э2.1): у сегмента берётся самая глубокая ступень с ≥25 событиями (у планшетов
оплат мало, но соединений тысячи), и пересчитывается вниз через коэффициенты
всего моста. Ошибка оценки наследуется от лестницы и едет в строку.

Мост покрывает ~56 % лидов и смещён (это лиды с client_id — веб). Поэтому
переносится ОТНОШЕНИЕ сегмента к базе моста, а не уровни; отношение на
кабинет переносимо, пока состав сегментов внутри моста похож на кабинетный.

Ключи устройств: Метрика отдаёт русские категории, Директ — DEVICE-ключи.
Сегмент без качества (нет в мосте, ТВ и т.п.) остаётся с чистой конверсией —
отсутствие данных не должно тихо обнулять корректировку.
"""

from math import sqrt
from typing import Any, Dict, List, Optional, Tuple

from sync.agent.computed import bid_modifier_percent
from sync.agent.ladder import MIN_STEP_EVENTS, ladder

# Метрика (edu_visit_behavior.device_category) → ключ сегмента Директа.
METRIKA_TO_DIRECT_DEVICE = {
    "ПК": "DESKTOP",
    "Смартфоны": "MOBILE",
    "Планшеты": "TABLET",
}

MIN_BRIDGE_LEADS = 1000  # мост мельче — отношения сегментов не оценить
NO_BRIDGE_REASON = (
    f"мост лид→устройство меньше {MIN_BRIDGE_LEADS} лидов — "
    "качество сегментов не считается, корректировки остаются на чистой конверсии"
)


def _counts(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    return {
        "leads": float(len(rows)),
        "eff": float(sum(1 for r in rows if r.get("is_eff"))),
        "connected": float(sum(1 for r in rows if r.get("is_connected"))),
        "deals": float(sum(1 for r in rows if r.get("is_deal"))),
        "paid": float(sum(1 for r in rows if r.get("is_paid"))),
    }


def device_quality_ratios(
    bridge_rows: List[Dict[str, Any]],
) -> Tuple[Dict[str, Dict[str, Any]], Optional[str]]:
    """Отношение ожидаемой выручки на лид сегмента к базе моста.

    bridge_rows — лиды моста: device (категория Метрики), is_eff, is_connected,
    is_deal, is_paid, amount. Возвращает ({DIRECT-ключ: {ratio, step, ...}},
    причина отказа) — одно из двух пусто.
    """
    if len(bridge_rows) < MIN_BRIDGE_LEADS:
        return {}, NO_BRIDGE_REASON

    total_counts = _counts(bridge_rows)
    if total_counts["paid"] < MIN_STEP_EVENTS:
        return {}, (
            "в мосте меньше "
            f"{int(MIN_STEP_EVENTS)} оплат — база выручки на лид не считается")
    total_revenue = sum(
        float(r.get("amount") or 0) for r in bridge_rows if r.get("is_paid"))
    avg_check = total_revenue / total_counts["paid"]
    base_rpl = total_revenue / total_counts["leads"]

    pools = (("bridge", total_counts),)
    out: Dict[str, Dict[str, Any]] = {}
    for metrika_key, direct_key in METRIKA_TO_DIRECT_DEVICE.items():
        rows = [r for r in bridge_rows if r.get("device") == metrika_key]
        if not rows:
            continue
        counts = _counts(rows)
        result = ladder(counts, pools, avg_check=avg_check)
        if result["step"] is None or result.get("expected_payments") is None:
            continue
        rpl = result["expected_payments"] * avg_check / counts["leads"]
        out[direct_key] = {
            "ratio": round(rpl / base_rpl, 4),
            "step": result["step"],
            "events": result["events"],
            "rel_error": result["rel_error"],
            "leads": int(counts["leads"]),
        }
    if not out:
        return {}, "ни один сегмент моста не набрал рабочей ступени"
    return out, None


def apply_quality_to_modifiers(
    rows: List[Dict[str, Any]],
    quality: Dict[str, Dict[str, Any]],
    setting_kind: str = "bid_modifier:device",
) -> int:
    """Домножает device-корректировки на качество лида сегмента. In-place.

    Возвращает число поправленных строк. Строки других срезов и сегменты без
    качества не трогаются. raw_value до поправки сохраняется в conv_ratio —
    обе компоненты обязаны быть видны в отчёте прогона по отдельности.
    """
    adjusted = 0
    for r in rows:
        if r.get("setting_kind") != setting_kind:
            continue
        q = quality.get(str(r.get("setting_key")))
        if not q:
            continue
        conv_ratio = float(r["raw_value"])
        combined = conv_ratio * float(q["ratio"])
        r["conv_ratio"] = conv_ratio
        r["quality_ratio"] = q["ratio"]
        r["raw_value"] = round(combined, 4)
        r["value"] = float(bid_modifier_percent(combined))
        # Ошибка произведения независимых оценок: относительные дисперсии
        # складываются. Если у конверсионной части ошибки нет — комбинированная
        # тоже неизвестна: качество не делает неизвестную оценку известной.
        conv_rel, q_rel = r.get("rel_error"), q.get("rel_error")
        if conv_rel is not None and q_rel is not None:
            r["rel_error"] = round(sqrt(float(conv_rel) ** 2 + float(q_rel) ** 2), 4)
        else:
            r["rel_error"] = None
        adjusted += 1
    return adjusted
