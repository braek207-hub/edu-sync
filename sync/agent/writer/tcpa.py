# -*- coding: utf-8 -*-
"""
sync/agent/writer/tcpa.py — Э3.5 (запись): рычаг целевого CPA.

Тот же конвейер, что у бюджета (writer/budget.py): план из computed-строк →
разница с ПРОЧИТАННЫМ состоянием кабинета → действие с previous_state и
ключом идемпотентности; дальше общие рельсы, красная линия, риск-бюджет и
сторож отката.

Отличий от бюджета два, и оба содержательные:

  * БАЗА КАПА. У бюджета шаг капится от РАСХОДА: недельный лимит у
    большинства кампаний висит в разы выше расхода и ничего не связывает,
    ±20 % от такой декорации не меняют ничего. Цель CPA — ручка, которая
    расход связывает всегда (стратегия по ней и оптимизирует), поэтому шаг
    капится от самой цели, стоящей в кабинете.
  * НЕДОДЕРЖАНИЕ цели уже учтено расчётом (agent/tcpa.py): здесь цель
    берётся как есть.

Блок BiddingStrategy уходит в update ЦЕЛИКОМ, как прочитан, с заменой одного
поля AverageCpa: структура в API заменяется, а не сливается по полям, и
пересборка потеряла бы соседние настройки (недельный лимит, цель конверсии,
BidCeiling, ExplorationBudget), которые ставил человек.
"""

import copy
import hashlib
from typing import Any, Dict, List, Optional, Tuple

from sync.agent.confidence import assess

MICROS = 1_000_000

TCPA_KIND = "tcpa.set"

# Кап шага НА ЗАПИСИ — от текущей цели (см. докстринг). Тот же размер, что у
# бюджета: правило спеки «не больше ±20 % за такт» общее для денежных ручек.
MAX_TCPA_STEP = 0.20

# Сдвиг меньше этого — не действие: цена такой правки (перезапуск обучения
# стратегии) выше выигрыша.
MIN_SHIFT = 0.05

# «Уже стоит»: расхождение факта с планом меньше этого — не действие.
ALREADY_SET_TOLERANCE = 0.05

LOW_CONFIDENCE_REASON = (
    "уверенность в экономическом преимуществе новой цели (допустимый "
    "предельный CPA против фактического) ниже порога класса budget_shift"
)
NO_TARGET_HOLDER_REASON = (
    "у стратегии кампании нет носителя цели CPA — рычаг к ней неприменим"
)
PACKAGE_REASON = (
    "кампания в пакетной стратегии {strategy_id}: цель задаётся на пакет, "
    "правка одной кампании тронула бы соседние"
)
NOT_TEXT_REASON = "не текстовая кампания: структура стратегии другая"


def _idempotency_key(campaign_id: str, micros: int) -> str:
    raw = f"tcpa:{campaign_id}:{micros}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _target_holder(block: Any) -> Optional[Dict[str, Any]]:
    """Вложенный словарь блока канала, несущий AverageCpa.

    Поиск по СОДЕРЖИМОМУ, а не по имени подблока, — тот же довод, что у
    _limit_holder бюджета: имя выводится из типа стратегии, и справочник имён
    пришлось бы править на каждый новый тип, а забытая правка означала бы
    молчаливый пропуск.
    """
    if not isinstance(block, dict):
        return None
    for value in block.values():
        if isinstance(value, dict) and "AverageCpa" in value:
            return value
    return None


def read_target_cpa(strategy: Dict[str, Any]) -> Optional[int]:
    """Текущая цель CPA в микрорублях из блока BiddingStrategy, или None."""
    for channel in ("Search", "Network"):
        holder = _target_holder((strategy or {}).get(channel))
        if holder is not None:
            try:
                return int(holder["AverageCpa"])
            except (TypeError, ValueError):
                return None
    return None


def strategy_with_target(strategy: Dict[str, Any], micros: int) -> Dict[str, Any]:
    """Копия блока BiddingStrategy с новой целью CPA."""
    out = copy.deepcopy(strategy)
    for channel in ("Search", "Network"):
        holder = _target_holder(out.get(channel))
        if holder is not None:
            holder["AverageCpa"] = int(micros)
            return out
    raise ValueError("в стратегии нет носителя AverageCpa")


def clamp_step(target_rub: float, current_rub: float,
               max_step: float = MAX_TCPA_STEP) -> int:
    """Цель, дожатая до ±max_step от ТЕКУЩЕЙ цели, в микрорублях.

    Округление до целого рубля: цель — управляющая ручка, и хвост в
    микрорублях делал бы ключ идемпотентности чувствительным к шуму
    плавающей точки (тот же довод, что у desired_weekly_micros бюджета).
    """
    lo = current_rub * (1.0 - max_step)
    hi = current_rub * (1.0 + max_step)
    return int(round(min(max(float(target_rub), lo), hi))) * MICROS


def plan_tcpa_moves(
    computed_by_campaign: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Строки Э3.5 → желаемые цели по кампаниям + счётчики молчания.

    Уверенность гейтуется ЗАНОВО и по ЭКОНОМИЧЕСКОМУ отношению
    (roi_vs_target), а не по размеру шага: тот же инвариант, что у бюджета
    после аудита 2026-08-23 (C2). Строка без экономического отношения —
    уверенность неизвестна, применять нельзя.
    """
    desired: Dict[str, Dict[str, Any]] = {}
    low_confidence: List[Dict[str, Any]] = []
    small_shift = 0
    confidence_unknown = 0

    for cid, rows in computed_by_campaign.items():
        row = next((r for r in rows
                    if str(r.get("setting_kind")) == "tcpa_target"
                    and str(r.get("setting_key")) == "target"), None)
        if row is None:
            continue
        target = float(row.get("value") or 0.0)
        current = float(row.get("raw_value") or 0.0)
        if target <= 0 or current <= 0:
            continue
        ratio = target / current
        if abs(ratio - 1.0) < MIN_SHIFT:
            small_shift += 1
            continue
        roi_row = next((r for r in rows
                        if str(r.get("setting_kind")) == "tcpa_target"
                        and str(r.get("setting_key")) == "roi_vs_target"), None)
        if roi_row is None or not float(roi_row.get("value") or 0.0) > 0:
            confidence_unknown += 1
            continue
        verdict = assess(float(roi_row["value"]), roi_row.get("rel_error"),
                         "budget_shift")
        if verdict["confident"] is None:
            confidence_unknown += 1
            continue
        if not verdict["confident"]:
            low_confidence.append({
                "campaign_id": str(cid), "ratio": round(ratio, 3),
                "roi_vs_target": round(float(roi_row["value"]), 4),
                "p_sign": verdict["p_sign"], "min_p_sign": verdict["min_p_sign"],
                "reason": LOW_CONFIDENCE_REASON,
            })
            continue
        desired[str(cid)] = {
            "target": target,
            "current": current,
            "ratio": round(ratio, 4),
            "roi_vs_target": round(float(roi_row["value"]), 4),
            "cpa_fact": roi_row.get("raw_value"),
            "p_sign": verdict["p_sign"],
        }

    return {
        "desired": desired,
        "small_shift": small_shift,
        "low_confidence": low_confidence,
        "confidence_unknown": confidence_unknown,
    }


def diff_tcpa(
    desired: Dict[str, Dict[str, Any]],
    actual_by_campaign: Dict[str, Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Желаемые цели × прочитанное состояние кабинета → (действия, отказы).

    actual_by_campaign — результат budget.fetch_budget_state: он уже читает
    BiddingStrategy свежим чтением, и второй поход в API за тем же был бы
    лишним. Кампании без записи в нём не порождают ни действия, ни отказа —
    их не оказалось в кабинете, и это видно счётчиком not_found вызывающего.
    """
    actions: List[Dict[str, Any]] = []
    refused: List[Dict[str, Any]] = []

    for cid in sorted(desired):
        move = desired[cid]
        state = actual_by_campaign.get(str(cid))
        if state is None:
            continue
        if state.get("package_id"):
            refused.append({"campaign_id": cid,
                            "reason": PACKAGE_REASON.format(
                                strategy_id=state["package_id"])})
            continue
        if state.get("campaign_type") not in (None, "TEXT_CAMPAIGN"):
            refused.append({"campaign_id": cid, "reason": NOT_TEXT_REASON})
            continue

        strategy = state.get("strategy")
        current_micros = (read_target_cpa(strategy)
                          if isinstance(strategy, dict) else None)
        if not current_micros:
            refused.append({"campaign_id": cid, "reason": NO_TARGET_HOLDER_REASON})
            continue

        # База капа — цель, СТОЯЩАЯ В КАБИНЕТЕ, а не та, из которой считал
        # расчёт: между прогонами цель правят руками, и шаг обязан меряться
        # от того, что там сейчас.
        target_micros = clamp_step(move["target"], current_micros / MICROS)
        if abs(target_micros - current_micros) < ALREADY_SET_TOLERANCE * current_micros:
            continue

        actions.append({
            "action_kind": TCPA_KIND,
            "object_level": "campaign",
            "object_id": str(cid),
            "direct_type": "AVERAGE_CPA",
            "key": "target_cpa",
            "payload": {
                "CampaignId": int(cid),
                "BiddingStrategy": strategy_with_target(strategy, target_micros),
                "TargetCpa": target_micros,
                # Для рельсы: фактический CPA окна, из которого посчитана цель.
                "CpaFact": move.get("cpa_fact"),
            },
            "previous_state": {
                "BiddingStrategy": strategy,
                "TargetCpa": current_micros,
            },
            "idempotency_key": _idempotency_key(str(cid), target_micros),
        })
    return actions, refused


def to_api_call(action: Dict[str, Any]) -> Tuple[str, str, Dict[str, Any]]:
    """Действие → вызов API. Тонкая обёртка над общим сборщиком apply."""
    from sync.agent.writer.apply import to_api_call as apply_to_api_call
    return apply_to_api_call(action)
