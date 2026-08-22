# -*- coding: utf-8 -*-
"""
sync/agent/hierarchy.py — уровень расчёта (Э2.2): кабинет → направление → кампания.

Сегментные корректировки считались по кабинету и раскатывались одинаково на все
кампании — это и держит ограничитель max_campaigns=1. Данные для различения есть:
покампанийные срезы (device — рабочий рычаг записи). Механика — частичный пулинг
в два шага:

    оценка кабинета     = наблюдение кабинета, сжатое к его базе (как раньше);
    оценка направления  = наблюдение направления, сжатое к ОЦЕНКЕ КАБИНЕТА;
    оценка кампании     = наблюдение кампании, сжатое к ОЦЕНКЕ НАПРАВЛЕНИЯ.

Чем больше своих кликов у узла, тем меньше он занимает у родителя (вес — те же
prior_trials из computed.py, в СОБЫТИЯХ, а не в наблюдениях). Между уровнями
переносится ОТНОШЕНИЕ к своей базе, а не сырая конверсия: базовая конверсия
кабинета и кампании различаются в разы, и перенос сырой конверсии выдал бы
кампании с сильной базой заниженные сегменты.

Доля собственных данных узла (pool_weight) едет в каждую строку: слой уверенности
(Э2.3) обязан отличать «кампания сама это показала» от «занято у направления».

Модуль чистый: счётчики собирает вызывающий.
"""

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from sync.agent.computed import (
    MIN_SUPPORT,
    NO_CLICKS_REASON,
    NO_CONVERSIONS_REASON,
    bid_modifier_percent,
    prior_trials,
)

# Ключ хвоста из collapse_tail: агрегат мелких сегментов, корректировку на него
# не выставить — реального сегмента с таким именем в кабинете нет.
TAIL_KEY = "other"


def shrink_toward(
    obs_conv: float, n: float, target_conv: float, prior_base_conv: float,
) -> float:
    """Наблюдаемая конверсия узла, сжатая к целевой (оценке родителя).

    prior_base_conv — база РОДИТЕЛЯ, не самого узла: вес приора переводится из
    событий в наблюдения через ожидаемую конверсию, и у тонкого узла своя база —
    шум. Кампания с 40 кликами и случайными 20 конверсиями получала базу 27 %,
    крошечный приор и вес 0.85 — то есть сжатие выключалось ровно там, где оно
    нужнее всего.
    """
    if n <= 0:
        return target_conv
    weight = n / (n + prior_trials(prior_base_conv))
    return weight * obs_conv + (1.0 - weight) * target_conv


def _own_weight(n: float, prior_base_conv: float) -> float:
    """Доля собственных данных узла в его оценке."""
    if n <= 0 or prior_base_conv <= 0:
        return 0.0
    return n / (n + prior_trials(prior_base_conv))


def _aggregate(
    rows: List[Dict[str, Any]], key_fn,
) -> Dict[Any, Dict[str, Dict[str, float]]]:
    """{ключ_уровня: {сегмент: {clicks, conversions}}}, хвост отброшен."""
    out: Dict[Any, Dict[str, Dict[str, float]]] = defaultdict(
        lambda: defaultdict(lambda: {"clicks": 0.0, "conversions": 0.0}))
    for r in rows:
        segment = str(r["slice_key"])
        if segment == TAIL_KEY:
            continue
        slot = out[key_fn(r)][segment]
        slot["clicks"] += float(r.get("clicks") or 0)
        slot["conversions"] += float(r.get("conversions") or 0)
    return out


def _base(segments: Dict[str, Dict[str, float]]) -> Tuple[float, float]:
    clicks = sum(s["clicks"] for s in segments.values())
    conv = sum(s["conversions"] for s in segments.values())
    return (conv / clicks) if clicks > 0 else 0.0, clicks


def hierarchical_modifiers(
    rows: List[Dict[str, Any]],
    direction_of: Dict[str, str],
    slice_kind: str,
) -> Tuple[Dict[str, List[Dict[str, Any]]], Optional[str]]:
    """Покампанийные корректировки одного кабинета и одного среза.

    rows — недельные строки среза кабинета (slices.build_sliced_facts):
    campaign_id, slice_key, clicks, conversions. Возвращает
    ({campaign_id: [строки настроек]}, причина отказа) — одно из двух пусто.

    Строка выходит только для кампании, у которой по сегменту ≥ MIN_SUPPORT
    СВОИХ кликов: у остальных личная оценка ≈ оценке направления, и их строки
    лишь дублировали бы уровень выше. Применение к таким кампаниям остаётся
    за кабинетным уровнем.
    """
    by_campaign = _aggregate(rows, lambda r: str(r["campaign_id"]))
    by_direction = _aggregate(
        rows, lambda r: direction_of.get(str(r["campaign_id"])) or "БЕЗ_НАПРАВЛЕНИЯ")
    account_segments: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"clicks": 0.0, "conversions": 0.0})
    for segments in by_campaign.values():
        for segment, counts in segments.items():
            account_segments[segment]["clicks"] += counts["clicks"]
            account_segments[segment]["conversions"] += counts["conversions"]

    account_base, account_clicks = _base(account_segments)
    if account_clicks <= 0:
        return {}, NO_CLICKS_REASON
    if account_base <= 0:
        return {}, NO_CONVERSIONS_REASON

    # Оценка кабинета: отношение сегмента к базе, сжатое к 1 (как в computed.py).
    account_ratio: Dict[str, float] = {}
    for segment, counts in account_segments.items():
        obs = counts["conversions"] / counts["clicks"] if counts["clicks"] > 0 else 0.0
        est = shrink_toward(obs, counts["clicks"], account_base, account_base)
        account_ratio[segment] = est / account_base

    # Оценка направления: сжатие к оценке кабинета, перенесённой на базу направления.
    direction_ratio: Dict[Tuple[str, str], float] = {}
    for direction, segments in by_direction.items():
        d_base, _ = _base(segments)
        if d_base <= 0:
            continue  # направление без конверсий живёт на кабинетной оценке
        for segment, counts in segments.items():
            target = account_ratio.get(segment, 1.0) * d_base
            obs = counts["conversions"] / counts["clicks"] if counts["clicks"] > 0 else 0.0
            est = shrink_toward(obs, counts["clicks"], target, account_base)
            direction_ratio[(direction, segment)] = est / d_base

    out: Dict[str, List[Dict[str, Any]]] = {}
    for campaign_id, segments in by_campaign.items():
        c_base, c_clicks = _base(segments)
        if c_base <= 0:
            continue  # без своих конверсий кампания живёт на уровне кабинета
        direction = direction_of.get(campaign_id) or "БЕЗ_НАПРАВЛЕНИЯ"
        prior_base, _ = _base(by_direction.get(direction, {}))
        if prior_base <= 0:
            prior_base = account_base
        rows_out: List[Dict[str, Any]] = []
        for segment, counts in segments.items():
            if counts["clicks"] < MIN_SUPPORT:
                continue
            parent = direction_ratio.get(
                (direction, segment), account_ratio.get(segment, 1.0))
            target = parent * c_base
            obs = counts["conversions"] / counts["clicks"]
            est = shrink_toward(obs, counts["clicks"], target, prior_base)
            ratio = est / c_base
            rows_out.append({
                "setting_kind": f"bid_modifier:{slice_kind}",
                "setting_key": segment,
                "value": float(bid_modifier_percent(ratio)),
                "support_n": int(counts["clicks"]),
                "raw_value": round(ratio, 4),
                "pool_weight": round(_own_weight(counts["clicks"], prior_base), 3),
            })
        if rows_out:
            out[campaign_id] = rows_out
    return out, None
