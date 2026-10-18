# -*- coding: utf-8 -*-
"""Коды отказов и ошибок модулей.

Python пишет только код, текст и совет рисует Panda (lib/modules/reasons.ts).
Список и порядок закреплены тестами в обоих репо: новый код без пары на той
стороне показал бы человеку «неизвестную причину».
"""

from typing import Optional, Tuple

REASON_CODES: Tuple[str, ...] = (
    # Защиты и границы записи.
    "in_never_list", "system_allowlist", "user_protect", "already_excluded_not_robot",
    "list_full", "campaign_type_unsupported", "campaign_stopped", "over_cap",
    # Защиты минус-фраз.
    "in_never_minus", "conflicts_keyword", "covered_by_minus", "family_pays_off",
    # Данные и доступ.
    "no_lead_goals", "goal_not_visible", "non_rub_money", "report_failed", "no_access",
    "direct_rejected", "too_many_goals", "rule_invalid",
)


def refusal(code: str, rule: Optional[str], *, login: Optional[str] = None,
            campaign_id: Optional[int] = None, obj: Optional[str] = None, **extra) -> dict:
    """Отказ с кодом из REASON_CODES: текст и совет к нему рисует Panda.

    Одна форма у всех модулей: раннер и экраны Panda читают отказы площадок и
    запросов одним кодом."""
    out = {"code": code, "object": obj, "campaign_id": campaign_id, "rule": rule,
           "login": login}
    out.update(extra)
    return out
